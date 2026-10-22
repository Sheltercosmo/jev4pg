"""Install the SQL queue without requiring files on the PostgreSQL host."""

import hashlib
from importlib.resources import files

from sqlalchemy import text

VERSION = "0.1.0"


class SQLInstallationError(ValueError):
    """A configuration conflict safe to show without connection credentials."""


def portable_script():
    source = (
        files("sdd").joinpath("postgres", f"jevsd_pg--{VERSION}.sql").read_text(encoding="utf-8")
    )
    return "\n".join(
        line
        for line in source.splitlines()
        if not line.startswith("SELECT pg_catalog.pg_extension_config_dump(")
    )


def installed(connection):
    version = connection.execute(
        text("SELECT extversion FROM pg_extension WHERE extname='jevsd_pg'")
    ).scalar()
    if version is not None:
        return {"mode": "extension", "version": version}
    marker = connection.execute(
        text(
            "SELECT c.relkind, c.relowner=n.nspowner AS same_owner FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='jev' AND c.relname='installation'"
        )
    ).first()
    if marker is None:
        return None
    if marker.relkind != "r" or not marker.same_owner:
        raise SQLInstallationError("The SQL installation marker has an unexpected kind or owner")
    rows = connection.execute(text("SELECT version, script_sha256 FROM jev.installation")).all()
    digest = hashlib.sha256(portable_script().encode()).hexdigest()
    if len(rows) != 1 or rows[0].version != VERSION or rows[0].script_sha256 != digest:
        raise SQLInstallationError("SQL interface version does not match this application")
    return {"mode": "portable", "version": rows[0].version}


def install(connection, mode, *, check=False):
    mode = "extension" if mode is True else mode
    if mode not in {"extension", "portable"}:
        raise SQLInstallationError("Choose extension or portable SQL installation")
    schema = connection.execute(
        text(
            "SELECT nspowner = (SELECT oid FROM pg_roles WHERE rolname=current_user) "
            "FROM pg_namespace WHERE nspname='jev'"
        )
    ).scalar()
    if schema is not None and not schema:
        raise SQLInstallationError(
            "The jev schema belongs to another owner; use its installation owner"
        )
    current = installed(connection) if schema is not None else None
    if current is not None:
        if current != {"mode": mode, "version": VERSION}:
            raise SQLInstallationError(
                "Keep the existing SQL installation mode and matching version"
            )
        if mode == "portable":
            owned = connection.execute(
                text(
                    "SELECT relowner = (SELECT oid FROM pg_roles WHERE rolname=current_user) "
                    "FROM pg_class WHERE oid='jev.installation'::regclass"
                )
            ).scalar_one()
            if not owned:
                raise SQLInstallationError("The SQL installation marker has an unexpected owner")
        return {**current, "action": "preserve"}
    if schema is not None:
        raise SQLInstallationError("Refusing to adopt an unmanaged jev schema")
    if not check:
        if mode == "extension":
            connection.exec_driver_sql(f"CREATE EXTENSION jevsd_pg VERSION '{VERSION}'")
        else:
            connection.exec_driver_sql("CREATE SCHEMA jev")
            connection.exec_driver_sql(portable_script())
            connection.exec_driver_sql(
                "CREATE TABLE jev.installation (singleton boolean PRIMARY KEY DEFAULT true "
                "CHECK (singleton), version text NOT NULL, script_sha256 text NOT NULL)"
            )
            connection.execute(
                text(
                    "INSERT INTO jev.installation(version, script_sha256) VALUES (:version,:digest)"
                ),
                {
                    "version": VERSION,
                    "digest": hashlib.sha256(portable_script().encode()).hexdigest(),
                },
            )
            connection.exec_driver_sql("REVOKE ALL ON jev.installation FROM PUBLIC")
    return {"mode": mode, "version": VERSION, "action": "create"}


def check_runtime(connection):
    current = installed(connection)
    if current is None or current["version"] != VERSION:
        raise ValueError(
            "Install the matching SQL interface using migrate --sql-interface portable or extension"
        )
    unsafe = connection.execute(
        text(
            "SELECT has_schema_privilege(current_user, oid, 'CREATE') "
            "OR pg_has_role(current_user, nspowner, 'MEMBER') "
            "FROM pg_namespace WHERE nspname='jev'"
        )
    ).scalar_one()
    if unsafe:
        raise ValueError("SQL workers must not own or create objects in the jev schema")
    return current
