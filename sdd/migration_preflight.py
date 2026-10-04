"""Read-only checks before adopting or upgrading an application installation."""

from sqlalchemy import inspect, text

from .schema import CATALOG_SCHEMA, metadata

VERSION_TWO_TABLES = {"native_query_admissions", "dataset_source_bindings"}
GUARD_FUNCTIONS = (
    "sdd_reject_evidence_update",
    "sdd_protect_concept_definition",
    "sdd_protect_feature_definition",
)


class InstallationConflict(ValueError):
    def __init__(self, conflicts):
        self.conflicts = sorted(set(conflicts))
        super().__init__("Installation conflicts: " + "; ".join(self.conflicts))


def inspect_installation(connection, runtime_role, target_version):
    from .generic import schema as generic_schema  # noqa: F401
    from .operators import schema as operator_schema  # noqa: F401

    conflicts = []
    role = connection.execute(
        text(
            "SELECT NOT rolcanlogin OR EXISTS (SELECT 1 FROM pg_roles parent "
            "WHERE pg_has_role(r.oid,parent.oid,'MEMBER') AND "
            "(parent.rolsuper OR parent.rolbypassrls OR parent.rolcreaterole "
            "OR parent.rolcreatedb OR parent.rolreplication)) "
            "FROM pg_roles r WHERE rolname=:role"
        ),
        {"role": runtime_role},
    ).scalar()
    if role:
        conflicts.append("runtime login must not have administrative role memberships")
    if (
        role is not None
        and connection.execute(
            text("SELECT pg_has_role(:role,current_user,'MEMBER')"), {"role": runtime_role}
        ).scalar_one()
    ):
        conflicts.append("migration must use a separate owner from the runtime login")

    namespaces = dict(
        connection.execute(
            text(
                "SELECT nspname,pg_get_userbyid(nspowner) FROM pg_namespace "
                "WHERE nspname IN ('sdd_catalog','sdd_data')"
            )
        ).all()
    )
    legacy_marker = connection.execute(
        text("SELECT to_regclass('public.sdd_schema_version') IS NOT NULL")
    ).scalar_one()
    catalog_exists = CATALOG_SCHEMA in namespaces
    if catalog_exists and legacy_marker:
        raise InstallationConflict(["both public installation marker and sdd_catalog exist"])
    catalog_schema = "public" if legacy_marker else CATALOG_SCHEMA
    tables = {table.name: table for table in metadata.tables.values()}
    objects = {
        row["relname"]: row
        for row in connection.execute(
            text(
                "SELECT c.oid,c.relname,c.relkind,c.relowner,c.relrowsecurity,c.relforcerowsecurity, "
                "pg_get_userbyid(c.relowner) AS owner FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=:schema"
            ),
            {"schema": catalog_schema},
        ).mappings()
    }
    functions = list(
        connection.execute(
            text(
                "SELECT p.proname,p.proowner,p.prorettype='trigger'::regtype AS is_trigger, "
                "l.lanname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                "JOIN pg_language l ON l.oid=p.prolang WHERE n.nspname=:schema "
                "AND p.pronargs=0 AND p.proname=ANY(:names)"
            ),
            {"names": list(GUARD_FUNCTIONS), "schema": catalog_schema},
        ).mappings()
    )
    data_owner = namespaces.get("sdd_data")
    marker = objects.get("sdd_schema_version")
    if marker is None:
        if catalog_exists:
            conflicts.append("sdd_catalog already exists without an installation marker")
        if data_owner is not None:
            conflicts.append("sdd_data already exists without an installation marker")
        if conflicts:
            raise InstallationConflict(conflicts)
        return {
            "status": "ready",
            "installation": "new",
            "schema_version": None,
            "catalog_schema": None,
            "target_version": target_version,
        }

    inspector = inspect(connection)
    marker_name = f"{catalog_schema}.sdd_schema_version"
    if marker["relkind"] != "r":
        raise InstallationConflict([f"{marker_name} must be an ordinary table"])
    columns = inspector.get_columns("sdd_schema_version", schema=catalog_schema)
    signature = [(c["name"], str(c["type"]), c["nullable"]) for c in columns]
    if signature != [("singleton", "BOOLEAN", False), ("version", "INTEGER", False)]:
        raise InstallationConflict([f"{marker_name} has an unexpected contract"])
    versions = connection.execute(text(f"SELECT singleton,version FROM {marker_name}")).all()
    supported = (1, 2) if legacy_marker else (3,)
    if len(versions) != 1 or versions[0][0] is not True or versions[0][1] not in supported:
        raise InstallationConflict([f"{marker_name} is not a supported installation"])
    previous = versions[0][1]
    expected = set(tables) - (VERSION_TWO_TABLES if previous == 1 else set())
    if catalog_exists and namespaces[CATALOG_SCHEMA] != marker["owner"]:
        conflicts.append("sdd_catalog and its tables must have the same owner")
    if (
        catalog_exists
        and role is not None
        and connection.execute(
            text("SELECT has_schema_privilege(:role,'sdd_catalog','CREATE')"),
            {"role": runtime_role},
        ).scalar_one()
    ):
        conflicts.append("runtime login must not have CREATE on sdd_catalog")
    owns_installation = (
        role is not None
        and connection.execute(
            text("SELECT pg_has_role(:role,:owner,'MEMBER')"),
            {"role": runtime_role, "owner": marker["relowner"]},
        ).scalar_one()
    )
    if owns_installation:
        conflicts.append("installation tables must have a separate owner from the runtime login")
    if data_owner != runtime_role:
        conflicts.append("sdd_data is missing or belongs to a different runtime login")

    policies = {}
    for row in connection.execute(
        text(
            "SELECT polrelid,polname,polcmd,polpermissive,polroles, "
            "pg_get_expr(polqual,polrelid) AS predicate, "
            "pg_get_expr(polwithcheck,polrelid) AS check_predicate FROM pg_policy"
        )
    ).mappings():
        policies.setdefault(row["polrelid"], []).append(row)

    for name, table in sorted(tables.items()):
        obj = objects.get(name)
        if name not in expected:
            if obj is not None:
                conflicts.append(f"{catalog_schema}.{name} predates its installation version")
            continue
        if obj is None or obj["relkind"] != "r" or obj["relowner"] != marker["relowner"]:
            conflicts.append(f"{catalog_schema}.{name} is missing or has a different kind or owner")
            continue
        actual = inspector.get_columns(name, schema=catalog_schema)
        expected_columns = [
            (c.name, _type_name(c.type, connection.dialect), c.nullable) for c in table.columns
        ]
        actual_columns = [
            (c["name"], _type_name(c["type"], connection.dialect), c["nullable"]) for c in actual
        ]
        primary_key = inspector.get_pk_constraint(name, schema=catalog_schema)[
            "constrained_columns"
        ]
        if actual_columns != expected_columns or primary_key != [c.name for c in table.primary_key]:
            conflicts.append(
                f"{catalog_schema}.{name} has an unexpected column or primary-key contract"
            )
        rules = policies.get(obj["oid"], [])
        if (
            not obj["relrowsecurity"]
            or not obj["relforcerowsecurity"]
            or len(rules) != 1
            or not _tenant_policy(rules[0])
        ):
            conflicts.append(f"{catalog_schema}.{name} does not have the installed tenant policy")
    if {row["proname"] for row in functions} != set(GUARD_FUNCTIONS):
        conflicts.append("installation guard functions are incomplete")
    elif any(
        row["proowner"] != marker["relowner"]
        or not row["is_trigger"]
        or row["lanname"] != "plpgsql"
        for row in functions
    ):
        conflicts.append("installation guard functions have different owners or contracts")
    if conflicts:
        raise InstallationConflict(conflicts)
    return {
        "status": "ready",
        "installation": "existing",
        "schema_version": previous,
        "catalog_schema": catalog_schema,
        "target_version": target_version,
    }


def _type_name(column_type, dialect):
    name = column_type.compile(dialect=dialect)
    return "DOUBLE PRECISION" if name == "FLOAT" else name


def _tenant_policy(policy):
    def normalize(expression):
        return (
            "".join((expression or "").replace("::text", "").split())
            .replace("(", "")
            .replace(")", "")
        )

    expected = "tenant=current_setting'sdd.tenant',true"
    return (
        policy["polname"] == "tenant_isolation"
        and policy["polcmd"] == "*"
        and policy["polpermissive"]
        and policy["polroles"] == [0]
        and normalize(policy["predicate"]) == expected
        and normalize(policy["check_predicate"]) == expected
    )
