"""Installation checks against disposable databases on an explicit test server."""

import os
import secrets
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from sdd.bootstrap import check_migration, migrate
from sdd.migration_preflight import InstallationConflict

pytestmark = pytest.mark.skipif(
    not os.getenv("SDD_TEST_ADMIN_URL"), reason="Dedicated admin test URL required"
)


@pytest.fixture
def target():
    base = make_url(os.environ["SDD_TEST_ADMIN_URL"])
    suffix = uuid.uuid4().hex[:12]
    database, role, parent = (f"migration_{kind}_{suffix}" for kind in ("db", "app", "parent"))
    admin = create_engine(base, isolation_level="AUTOCOMMIT", hide_parameters=True)
    with admin.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{database}"')
    url = base.set(database=database)
    engine = create_engine(url, hide_parameters=True)
    try:
        yield url, engine, role, parent
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
            connection.exec_driver_sql(f'DROP ROLE IF EXISTS "{role}"')
            connection.exec_driver_sql(f'DROP ROLE IF EXISTS "{parent}"')
        admin.dispose()


def objects(engine):
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT n.nspname,c.relname,c.relowner,c.relacl::text,c.relrowsecurity,c.relforcerowsecurity, "
                "p.polname,pg_get_expr(p.polqual,p.polrelid) "
                "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "LEFT JOIN pg_policy p ON p.polrelid=c.oid "
                "WHERE n.nspname IN ('public','sdd_catalog') ORDER BY n.nspname,c.relname,p.polname"
            )
        ).all()


@pytest.mark.parametrize("collision", ["table", "function", "schema", "marker"])
def test_unmanaged_objects_are_untouched(target, collision):
    url, engine, role, _ = target
    with engine.begin() as connection:
        if collision in {"table", "function"}:
            connection.exec_driver_sql("CREATE SCHEMA sdd_catalog")
        if collision == "table":
            connection.exec_driver_sql(
                "CREATE TABLE sdd_catalog.source_records(id text,tenant text)"
            )
            connection.exec_driver_sql(
                "INSERT INTO sdd_catalog.source_records VALUES('old','业务数据')"
            )
            connection.exec_driver_sql(
                "ALTER TABLE sdd_catalog.source_records ENABLE ROW LEVEL SECURITY"
            )
            connection.exec_driver_sql(
                "CREATE POLICY tenant_isolation ON sdd_catalog.source_records USING(true)"
            )
        elif collision == "function":
            connection.exec_driver_sql(
                "CREATE FUNCTION sdd_catalog.sdd_reject_evidence_update() RETURNS int LANGUAGE sql AS 'SELECT 9'"
            )
        elif collision == "schema":
            connection.exec_driver_sql("CREATE SCHEMA sdd_data")
        else:
            connection.exec_driver_sql(
                "CREATE TABLE public.sdd_schema_version(singleton boolean PRIMARY KEY,version integer NOT NULL)"
            )
            connection.exec_driver_sql("INSERT INTO public.sdd_schema_version VALUES(true,2)")
    before = objects(engine)
    for action in (check_migration, migrate):
        with pytest.raises(InstallationConflict):
            action(url, role)
        assert objects(engine) == before
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": role}
            ).scalar()
            is None
        )
        if collision == "table":
            assert connection.exec_driver_sql("SELECT * FROM sdd_catalog.source_records").all() == [
                ("old", "业务数据")
            ]
        elif collision == "function":
            assert (
                connection.exec_driver_sql(
                    "SELECT sdd_catalog.sdd_reject_evidence_update()"
                ).scalar_one()
                == 9
            )


def test_check_is_read_only_and_custom_search_path_cannot_redirect_installation(target):
    url, engine, role, _ = target
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE SCHEMA "团队"')
        connection.exec_driver_sql('CREATE TABLE "团队".source_records(id integer)')
        connection.exec_driver_sql('INSERT INTO "团队".source_records VALUES(42)')
    custom = url.update_query_dict({"options": "-csearch_path=团队,public"})
    assert check_migration(custom, role)["installation"] == "new"
    assert not objects(engine)
    migrate(custom, role, secrets.token_urlsafe(32))
    migrate(custom, role)
    assert check_migration(custom, role)["installation"] == "existing"
    with engine.connect() as connection:
        assert connection.exec_driver_sql('SELECT * FROM "团队".source_records').all() == [(42,)]
        assert not connection.execute(
            text(
                "SELECT relrowsecurity FROM pg_class WHERE oid='\"团队\".source_records'::regclass"
            )
        ).scalar_one()
        assert connection.execute(
            text("SELECT to_regclass('sdd_catalog.source_records') IS NOT NULL")
        ).scalar_one()


def test_inherited_admin_privilege_is_rejected_before_installation(target):
    url, engine, role, parent = target
    with engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE ROLE "{parent}" CREATEDB')
        connection.exec_driver_sql(f'CREATE ROLE "{role}" LOGIN NOINHERIT')
        connection.exec_driver_sql(f'GRANT "{parent}" TO "{role}"')
    with pytest.raises(InstallationConflict, match="administrative"):
        migrate(url, role)
    assert not objects(engine)


def test_runtime_cannot_own_a_fresh_installation(target):
    url, engine, role, _ = target
    password = secrets.token_urlsafe(32)
    with engine.begin() as connection:
        connection.exec_driver_sql(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}'")
        connection.exec_driver_sql(f'ALTER DATABASE "{url.database}" OWNER TO "{role}"')
    for action in (check_migration, migrate):
        with pytest.raises(InstallationConflict, match="separate owner"):
            action(url.set(username=role, password=password), role)
    assert not objects(engine)


@pytest.mark.parametrize("change", ["owner", "policy", "column", "version"])
def test_changed_installation_contract_is_not_silently_repaired(target, change):
    url, engine, role, parent = target
    migrate(url, role, secrets.token_urlsafe(32))
    with engine.begin() as connection:
        if change == "owner":
            connection.exec_driver_sql(f'CREATE ROLE "{parent}"')
            connection.exec_driver_sql(
                f'ALTER TABLE sdd_catalog.source_records OWNER TO "{parent}"'
            )
        elif change == "policy":
            connection.exec_driver_sql(
                "ALTER POLICY tenant_isolation ON sdd_catalog.source_records USING(true)"
            )
        elif change == "column":
            connection.exec_driver_sql(
                "ALTER TABLE sdd_catalog.source_records ADD COLUMN business_value text"
            )
        else:
            connection.exec_driver_sql("UPDATE sdd_catalog.sdd_schema_version SET version=999")
    before = objects(engine)
    with pytest.raises(InstallationConflict):
        migrate(url, role)
    assert objects(engine) == before
