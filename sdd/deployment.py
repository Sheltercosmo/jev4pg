"""Deployment readiness checks without model inference."""

import os
from sqlalchemy import text
from .bootstrap import SCHEMA_VERSION, NATIVE_EXTENSION_VERSION


def check_database(db, sql_interface=None):
    if db.engine.dialect.name != "postgresql":
        raise ValueError("Production requires PostgreSQL")
    with db.engine.connect() as connection:
        unsafe = connection.execute(
            text(
                "SELECT count(*) FROM pg_roles WHERE "
                "pg_has_role(current_user, oid, 'MEMBER') AND "
                "(rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb OR rolreplication)"
            )
        ).scalar_one()
        if unsafe:
            raise ValueError("Runtime must use a restricted PostgreSQL role")
        version = connection.execute(
            text("SELECT version FROM sdd_catalog.sdd_schema_version")
        ).scalar_one()
        if version != SCHEMA_VERSION:
            raise ValueError("Run the migration command for this application version")
        if connection.execute(
            text("SELECT has_schema_privilege(current_user,'sdd_catalog','CREATE')")
        ).scalar_one():
            raise ValueError("Runtime must not have CREATE on the application catalog schema")
        tables = connection.execute(
            text(
                "SELECT c.relrowsecurity, c.relforcerowsecurity, "
                "pg_has_role(current_user, c.relowner, 'MEMBER') AS owns_table "
                "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='sdd_catalog' AND c.relname='jev_operator_runs'"
            )
        ).first()
        if not tables or not tables[0] or not tables[1] or tables[2]:
            raise ValueError("Tenant policies must be installed by a separate schema owner")
        required = (
            sql_interface if sql_interface is not None else os.getenv("SDD_SQL_INTERFACE") == "1"
        )
        if required:
            extension = connection.execute(
                text("SELECT extversion FROM pg_extension WHERE extname='jevsd_pg'")
            ).scalar()
            if extension != "0.1.0":
                raise ValueError("Install the matching jevsd_pg SQL extension")
        if os.getenv("SDD_SEMANTIC_ENGINE") == "native":
            native = connection.execute(
                text("SELECT extversion FROM pg_extension WHERE extname='jev_native'")
            ).scalar()
            if native != NATIVE_EXTENSION_VERSION:
                raise ValueError("Install the matching jev_native Rust extension")
            admission = connection.execute(
                text(
                    "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class "
                    "WHERE oid=to_regclass('sdd_catalog.native_query_admissions')"
                )
            ).scalar()
            executable = connection.execute(
                text(
                    "SELECT has_schema_privilege(current_user,'jev_native','USAGE') "
                    "AND has_function_privilege(current_user,'jev_native.scan(text,jsonb,jsonb)','EXECUTE')"
                    " AND has_function_privilege(current_user,to_regprocedure('jev_native.scan_many(jsonb,jsonb)'),'EXECUTE')"
                    " AND has_function_privilege(current_user,to_regprocedure('jev_native.execute_plan(jsonb,jsonb)'),'EXECUTE')"
                    " AND has_function_privilege(current_user,to_regprocedure('jev_native.embed(text,jsonb,jsonb)'),'EXECUTE')"
                )
            ).scalar()
            if not admission or not executable:
                raise ValueError(
                    "Run migrate --native-interface to install native admission and runtime grants"
                )
    return {"database": "ready", "schema_version": version, "sql_interface": bool(required)}
