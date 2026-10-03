"""Install the application schema using an administrator connection."""

from importlib.resources import files

from sqlalchemy import text

from .db import Database
from .postgres_security import secure
from .schema import metadata

SCHEMA_VERSION = 1
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


def migrate(admin_url, runtime_role="sdd_app", runtime_password=None, sql_interface=False):
    db = Database(admin_url)
    if db.engine.dialect.name != "postgresql":
        raise ValueError("PostgreSQL is required for deployment")
    try:
        with db.engine.begin() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(1747654244, 1)"))
            ensure_login(connection, runtime_role, runtime_password)
            role = identifier(connection, runtime_role)
            from .generic import schema as generic_schema  # noqa: F401
            from .operators import schema as operator_schema  # noqa: F401

            metadata.create_all(connection)
            connection.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS public.sdd_schema_version "
                    "(singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton), "
                    "version integer NOT NULL)"
                )
            )
            previous = connection.execute(
                text("SELECT version FROM public.sdd_schema_version")
            ).scalar()
            if previous is not None and previous > SCHEMA_VERSION:
                raise ValueError("Database schema is newer than this application")
            connection.exec_driver_sql("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
            connection.exec_driver_sql(f"CREATE SCHEMA IF NOT EXISTS sdd_data AUTHORIZATION {role}")
            connection.exec_driver_sql(f"GRANT USAGE, CREATE ON SCHEMA sdd_data TO {role}")
            for table in metadata.sorted_tables:
                name = identifier(connection, table.name)
                permissions = (
                    "SELECT, INSERT, DELETE"
                    if table.name in IMMUTABLE_TABLES
                    else "SELECT, INSERT, UPDATE, DELETE"
                )
                connection.exec_driver_sql(f"GRANT {permissions} ON public.{name} TO {role}")
                if table.name in IMMUTABLE_TABLES:
                    connection.exec_driver_sql(f"REVOKE UPDATE ON public.{name} FROM {role}")
            secure(db, connection)
            for name in (
                "sdd_reject_evidence_update",
                "sdd_protect_concept_definition",
                "sdd_protect_feature_definition",
            ):
                connection.exec_driver_sql(f"REVOKE ALL ON FUNCTION public.{name}() FROM PUBLIC")
            if sql_interface:
                connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS jevsd_pg")
                grant_worker(connection, runtime_role)
            connection.execute(
                text(
                    "INSERT INTO public.sdd_schema_version(singleton, version) VALUES(true, :version) "
                    "ON CONFLICT (singleton) DO UPDATE SET version = EXCLUDED.version"
                ),
                {"version": SCHEMA_VERSION},
            )
            connection.exec_driver_sql(f"GRANT SELECT ON public.sdd_schema_version TO {role}")
        return {"schema_version": SCHEMA_VERSION, "sql_interface": sql_interface}
    finally:
        db.engine.dispose()


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
                text("SELECT has_table_privilege(:login, 'public.jev_operator_runs', 'SELECT')"),
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
