"""Install the application schema using an administrator connection."""

from importlib.resources import files

from sqlalchemy import text

from .db import Database
from .postgres_security import secure
from .schema import CATALOG_SCHEMA, metadata

SCHEMA_VERSION = 3
NATIVE_EXTENSION_VERSION = "0.2.0"
IMMUTABLE_TABLES = {
    "source_versions",
    "evaluator_revisions",
    "decision_policy_revisions",
    "observations",
    "human_assertions",
    "dataset_row_versions",
    "dataset_semantic_answers",
    "dataset_feature_assertions",
    "jev_operator_observations",
    "jev_operator_assertions",
    "jev_operator_definitions",
}


def identifier(connection, value):
    if not isinstance(value, str) or not value or len(value.encode()) > 63 or "\x00" in value:
        raise ValueError("Invalid PostgreSQL identifier")
    return connection.dialect.identifier_preparer.quote_identifier(value)


def ensure_login(connection, role, password=None):
    quoted = identifier(connection, role)
    existing = connection.execute(
        text(
            "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication "
            "FROM pg_roles WHERE rolname = :role"
        ),
        {"role": role},
    ).first()
    if existing:
        if any(existing):
            raise ValueError("The configured application role has administrative privileges")
        return False
    if not password or len(password) < 24:
        raise ValueError("A new application role needs a password of at least 24 characters")
    # PostgreSQL utility statements do not accept password bind parameters.
    literal = connection.execute(
        text("SELECT quote_literal(:value)"), {"value": password}
    ).scalar_one()
    try:
        connection.exec_driver_sql(
            f"CREATE ROLE {quoted} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            f"NOREPLICATION NOBYPASSRLS PASSWORD {literal}"
        )
    except Exception:
        raise ValueError(
            "Could not create the database login; check administrator permissions"
        ) from None
    return True


def migrate(
    admin_url,
    runtime_role="sdd_app",
    runtime_password=None,
    sql_interface=False,
    native_interface=False,
    native_registry=False,
    native_registry_password=None,
):
    if native_registry and not native_interface:
        raise ValueError("Native registry setup requires --native-interface")
    db = Database(admin_url)
    if db.engine.dialect.name != "postgresql":
        raise ValueError("PostgreSQL is required for deployment")
    try:
        with db.engine.begin() as connection:
            connection.exec_driver_sql("SET LOCAL search_path=pg_catalog,public,pg_temp")
            connection.execute(text("SELECT pg_advisory_xact_lock(1747654244, 1)"))
            from .migration_preflight import InstallationConflict, inspect_installation

            installation = inspect_installation(connection, runtime_role, SCHEMA_VERSION)
            ensure_login(connection, runtime_role, runtime_password)
            role = identifier(connection, runtime_role)
            from .generic import schema as generic_schema  # noqa: F401
            from .operators import schema as operator_schema  # noqa: F401

            prepare_catalog(connection, installation)
            create_catalog_tables(connection)
            connection.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS sdd_catalog.sdd_schema_version "
                    "(singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton), "
                    "version integer NOT NULL)"
                )
            )
            previous = connection.execute(
                text("SELECT version FROM sdd_catalog.sdd_schema_version")
            ).scalar()
            if previous is not None and previous > SCHEMA_VERSION:
                raise ValueError("Database schema is newer than this application")
            connection.exec_driver_sql("REVOKE ALL ON SCHEMA sdd_catalog FROM PUBLIC")
            connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA sdd_catalog TO {role}")
            if connection.execute(
                text("SELECT has_schema_privilege(:role,'sdd_catalog','CREATE')"),
                {"role": runtime_role},
            ).scalar_one():
                raise InstallationConflict(
                    ["default or inherited grants give the runtime login CREATE on sdd_catalog"]
                )
            connection.exec_driver_sql(f"CREATE SCHEMA IF NOT EXISTS sdd_data AUTHORIZATION {role}")
            connection.exec_driver_sql(f"GRANT USAGE, CREATE ON SCHEMA sdd_data TO {role}")
            for table in metadata.sorted_tables:
                name = identifier(connection, table.name)
                permissions = (
                    "SELECT, INSERT, DELETE"
                    if table.name in IMMUTABLE_TABLES
                    else "SELECT, INSERT, UPDATE, DELETE"
                )
                connection.exec_driver_sql(f"GRANT {permissions} ON sdd_catalog.{name} TO {role}")
                if table.name in IMMUTABLE_TABLES:
                    connection.exec_driver_sql(f"REVOKE UPDATE ON sdd_catalog.{name} FROM {role}")
            secure(db, connection)
            for name in (
                "sdd_reject_evidence_update",
                "sdd_protect_concept_definition",
                "sdd_protect_feature_definition",
            ):
                connection.exec_driver_sql(
                    f"REVOKE ALL ON FUNCTION sdd_catalog.{name}() FROM PUBLIC"
                )
            if sql_interface:
                connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS jevsd_pg")
                grant_worker(connection, runtime_role)
            if native_interface:
                connection.exec_driver_sql(
                    f"CREATE EXTENSION IF NOT EXISTS jev_native VERSION '{NATIVE_EXTENSION_VERSION}'"
                )
                installed = connection.execute(
                    text("SELECT extversion FROM pg_extension WHERE extname='jev_native'")
                ).scalar_one()
                if installed == "0.1.0":
                    connection.exec_driver_sql(
                        f"ALTER EXTENSION jev_native UPDATE TO '{NATIVE_EXTENSION_VERSION}'"
                    )
                elif installed != NATIVE_EXTENSION_VERSION:
                    raise ValueError(
                        "Install the native extension version matching this application"
                    )
                connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA jev_native TO {role}")
                connection.exec_driver_sql(
                    f"GRANT EXECUTE ON FUNCTION jev_native.scan(text,jsonb,jsonb) TO {role}"
                )
                connection.exec_driver_sql(
                    f"GRANT EXECUTE ON FUNCTION jev_native.scan_many(jsonb,jsonb) TO {role}"
                )
                connection.exec_driver_sql(
                    f"GRANT EXECUTE ON FUNCTION jev_native.execute_plan(jsonb,jsonb) TO {role}"
                )
                connection.exec_driver_sql(
                    f"GRANT EXECUTE ON FUNCTION jev_native.embed(text,jsonb,jsonb) TO {role}"
                )
                if native_registry:
                    grant_native_registry(connection, native_registry_password)
            connection.execute(
                text(
                    "INSERT INTO sdd_catalog.sdd_schema_version(singleton, version) VALUES(true, :version) "
                    "ON CONFLICT (singleton) DO UPDATE SET version = EXCLUDED.version"
                ),
                {"version": SCHEMA_VERSION},
            )
            connection.exec_driver_sql(f"GRANT SELECT ON sdd_catalog.sdd_schema_version TO {role}")
        result = {"schema_version": SCHEMA_VERSION, "sql_interface": sql_interface}
        if native_interface:
            result["native_interface"] = True
        if native_registry:
            result["native_registry"] = True
        return result
    finally:
        db.engine.dispose()


def check_migration(admin_url, runtime_role="sdd_app"):
    from .migration_preflight import inspect_installation

    db = Database(admin_url)
    try:
        if db.engine.dialect.name != "postgresql":
            raise ValueError("PostgreSQL is required for deployment")
        with db.engine.begin() as connection:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            connection.exec_driver_sql("SET LOCAL search_path=pg_catalog,public,pg_temp")
            return inspect_installation(connection, runtime_role, SCHEMA_VERSION)
    finally:
        db.engine.dispose()


def prepare_catalog(connection, installation):
    """Move a validated legacy catalog, preserving relation identity and dependencies."""
    if installation["catalog_schema"] == CATALOG_SCHEMA:
        return
    if installation["catalog_schema"] is None:
        connection.exec_driver_sql("CREATE SCHEMA sdd_catalog")
        return

    from .migration_preflight import GUARD_FUNCTIONS, VERSION_TWO_TABLES

    owner = connection.execute(
        text(
            "SELECT pg_get_userbyid(relowner) FROM pg_class "
            "WHERE oid='public.sdd_schema_version'::regclass"
        )
    ).scalar_one()
    connection.exec_driver_sql(
        f"CREATE SCHEMA sdd_catalog AUTHORIZATION {identifier(connection, owner)}"
    )
    for table in metadata.sorted_tables:
        if installation["schema_version"] == 1 and table.name in VERSION_TWO_TABLES:
            continue
        name = identifier(connection, table.name)
        connection.exec_driver_sql(f"ALTER TABLE public.{name} SET SCHEMA sdd_catalog")
    for name in GUARD_FUNCTIONS:
        connection.exec_driver_sql(f"ALTER FUNCTION public.{name}() SET SCHEMA sdd_catalog")
    connection.exec_driver_sql("ALTER TABLE public.sdd_schema_version SET SCHEMA sdd_catalog")


def create_catalog_tables(connection):
    owner, migrator = connection.execute(
        text(
            "SELECT pg_get_userbyid(nspowner),current_user FROM pg_namespace "
            "WHERE nspname='sdd_catalog'"
        )
    ).one()
    if owner != migrator:
        connection.exec_driver_sql(f"SET LOCAL ROLE {identifier(connection, owner)}")
    metadata.create_all(connection)
    if owner != migrator:
        connection.exec_driver_sql(f"SET LOCAL ROLE {identifier(connection, migrator)}")


def grant_native_registry(connection, password):
    login = "jev_registry"
    ensure_login(connection, login, password)
    unsafe = connection.execute(
        text(
            "SELECT NOT rolcanlogin OR EXISTS (SELECT 1 FROM pg_roles r "
            "WHERE r.rolname<>:role AND pg_has_role(:role,r.oid,'MEMBER')) "
            "OR EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema' "
            "AND c.relkind IN ('r','p','v','m','f') "
            "AND has_table_privilege(:role,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE')) "
            "FROM pg_roles WHERE rolname=:role"
        ),
        {"role": login},
    ).scalar_one()
    if unsafe:
        raise ValueError(
            "Native registry needs a dedicated login without memberships or source access"
        )
    connection.exec_driver_sql("ALTER ROLE jev_registry CONNECTION LIMIT 8")
    connection.exec_driver_sql("GRANT USAGE ON SCHEMA jev_native TO jev_registry")
    for signature in (
        "_registry_lookup(text,text[],integer)",
        "_registry_claim(text,text,text,integer,integer,integer,integer,boolean)",
        "_registry_finish(uuid,jsonb,text)",
    ):
        connection.exec_driver_sql(
            f"GRANT EXECUTE ON FUNCTION jev_native.{signature} TO jev_registry"
        )


def grant_worker(connection, runtime_role):
    role = identifier(connection, runtime_role)
    connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA jev TO {role}")
    for signature in (
        "_claim(uuid, integer)",
        "_heartbeat(uuid, uuid, integer)",
        "_finish(uuid, uuid, jsonb)",
    ):
        connection.exec_driver_sql(f"GRANT EXECUTE ON FUNCTION jev.{signature} TO {role}")


def extension_files():
    return files("sdd").joinpath("postgres")


def grant_client(admin_url, login, tenant, actor, access="reader"):
    if access not in {"reader", "reviewer"} or not tenant or len(tenant) > 100 or not actor:
        raise ValueError("Provide a tenant, actor and reader or reviewer access")
    db = Database(admin_url)
    try:
        with db.engine.begin() as connection:
            role = identifier(connection, login)
            exists = connection.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": login}
            ).scalar()
            if not exists:
                raise ValueError("Create the PostgreSQL login before granting JEV access")
            ensure_login(connection, login)
            if connection.execute(
                text(
                    "SELECT has_table_privilege(:login, 'sdd_catalog.jev_operator_runs', 'SELECT')"
                ),
                {"login": login},
            ).scalar_one():
                raise ValueError("Use a separate SQL client login without runtime table grants")
            connection.execute(
                text(
                    "INSERT INTO jev.client_roles(login, tenant, actor, access) "
                    "VALUES(:login, :tenant, :actor, :access) ON CONFLICT(login) DO UPDATE "
                    "SET tenant=excluded.tenant, actor=excluded.actor, access=excluded.access"
                ),
                {"login": login, "tenant": tenant, "actor": actor, "access": access},
            )
            connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA jev TO {role}")
            for signature in (
                "submit(text, jsonb, jsonb, jsonb, text, text)",
                "result(uuid)",
                "cancel(uuid)",
            ):
                connection.exec_driver_sql(f"GRANT EXECUTE ON FUNCTION jev.{signature} TO {role}")
    finally:
        db.engine.dispose()
