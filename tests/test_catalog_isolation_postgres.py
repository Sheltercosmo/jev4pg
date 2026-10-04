"""Catalog relocation and coexistence use real PostgreSQL object identities."""

import os
import secrets

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from sdd import schema
from sdd.bootstrap import SCHEMA_VERSION, check_migration, migrate
from sdd.db import Database
from sdd.deployment import check_database
from sdd.generic.catalog import Catalog
from sdd.generic.history import QueryHistory
from sdd.generic.sql import SQLService
from sdd.ledger import Ledger
from sdd.migration_preflight import (
    GUARD_FUNCTIONS,
    VERSION_TWO_TABLES,
    VERSION_FOUR_TABLES,
    InstallationConflict,
)
from sdd.operators.service import OperatorService
from test_migration_preflight_postgres import target as target
from test_operator_runtime import Model

pytestmark = pytest.mark.skipif(
    not os.getenv("SDD_TEST_ADMIN_URL"), reason="Dedicated admin test URL required"
)


def public_contract(engine):
    with engine.connect() as connection:
        return (
            connection.exec_driver_sql(
                "SELECT nspowner,nspacl::text FROM pg_namespace WHERE nspname='public'"
            ).all(),
            connection.exec_driver_sql(
                "SELECT c.oid,c.relname,c.relowner,c.relacl::text,c.relrowsecurity,"
                "c.relforcerowsecurity,p.polname,pg_get_expr(p.polqual,p.polrelid) "
                "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "LEFT JOIN pg_policy p ON p.polrelid=c.oid "
                "WHERE n.nspname='public' ORDER BY c.oid,p.polname"
            ).all(),
            connection.exec_driver_sql(
                "SELECT p.oid,p.proowner,p.proacl::text,pg_get_functiondef(p.oid) "
                "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname='public' ORDER BY p.oid"
            ).all(),
        )


def test_installation_coexists_with_public_objects_and_runtime_shadow_tables(target):
    url, engine, role, _ = target
    password = secrets.token_urlsafe(32)
    with engine.begin() as connection:
        connection.exec_driver_sql("GRANT CREATE ON SCHEMA public TO PUBLIC")
        connection.exec_driver_sql("CREATE TABLE public.source_records(id integer, note text)")
        connection.exec_driver_sql("INSERT INTO public.source_records VALUES(42,'业务数据')")
        connection.exec_driver_sql("ALTER TABLE public.source_records ENABLE ROW LEVEL SECURITY")
        connection.exec_driver_sql(
            "CREATE POLICY business_read ON public.source_records USING(true)"
        )
        connection.exec_driver_sql(
            "CREATE FUNCTION public.sdd_reject_evidence_update() RETURNS int LANGUAGE sql AS 'SELECT 9'"
        )
        connection.exec_driver_sql('CREATE SCHEMA "业务"')
        connection.exec_driver_sql('CREATE TABLE "业务".dataset_catalog(id integer)')
        connection.exec_driver_sql('INSERT INTO "业务".dataset_catalog VALUES(99)')
    before = public_contract(engine)
    assert check_migration(url, role)["installation"] == "new"
    migrate(url, role, password)
    migrate(url, role)
    assert public_contract(engine) == before
    with engine.begin() as connection:
        connection.exec_driver_sql(f'GRANT USAGE ON SCHEMA "业务" TO "{role}"')
        connection.exec_driver_sql(
            f'GRANT SELECT ON public.source_records,"业务".dataset_catalog TO "{role}"'
        )
    runtime = Database(
        url.set(username=role, password=password).update_query_dict(
            {"options": "-csearch_path=业务,public"}
        )
    )
    try:
        assert check_database(runtime)["schema_version"] == SCHEMA_VERSION
        catalog = Catalog(runtime)
        dataset = catalog.create(
            "tenant-a", "records", [{"id": 1, "note": "完成"}], primary_key=["id"]
        )
        assert [row["id"] for row in catalog.list("tenant-a")] == [dataset["id"]]
        assert catalog.list("tenant-b") == []
        assert SQLService(runtime).execute("tenant-a", "SELECT note FROM records")["result"] == [
            {"note": "完成"}
        ]
        assert (
            OperatorService(runtime, Model(), "tenant-a", "r", "reviewer").call(
                "NOUL", {"state": "记录", "proposition": "是否完成？"}
            )["output_state"]
            == "VALUE"
        )
        attached = catalog.attach("tenant-a", "business", "public", "source_records")
        assert catalog.rows("tenant-a", attached) == [{"id": 42, "note": "业务数据"}]
        with pytest.raises(ValueError, match="internal"):
            catalog.attach("tenant-a", "private", "sdd_catalog", "dataset_catalog")
        with engine.begin() as connection:
            connection.exec_driver_sql(
                'CREATE VIEW "业务".metadata_proxy WITH (security_invoker=true) AS SELECT * FROM sdd_catalog.dataset_catalog'
            )
            connection.exec_driver_sql(f'GRANT SELECT ON "业务".metadata_proxy TO "{role}"')
        with pytest.raises(ValueError, match="internal"):
            catalog.attach("tenant-a", "private_view", "业务", "metadata_proxy")
        with runtime.transaction("tenant-a") as connection:
            connection.exec_driver_sql("CREATE TEMP TABLE dataset_catalog(id integer)")
            connection.exec_driver_sql("SET LOCAL search_path=pg_temp,public")
            from sdd.generic.schema import datasets

            assert len(connection.execute(select(datasets)).all()) == 2
        with pytest.raises(DBAPIError), runtime.engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE sdd_catalog.unauthorized(id integer)")
        with engine.connect() as connection:
            assert connection.exec_driver_sql('SELECT * FROM "业务".dataset_catalog').all() == [
                (99,)
            ]
            assert (
                connection.exec_driver_sql(
                    "SELECT public.sdd_reject_evidence_update()"
                ).scalar_one()
                == 9
            )
    finally:
        runtime.engine.dispose()


def legacy_layout(engine, version):
    """Build old layouts for rollback fault injection; released-source upgrades run separately."""
    with engine.begin() as connection:
        for table in schema.metadata.sorted_tables:
            if table.name in VERSION_FOUR_TABLES or (
                version == 1 and table.name in VERSION_TWO_TABLES
            ):
                connection.exec_driver_sql(f'DROP TABLE sdd_catalog."{table.name}"')
            else:
                connection.exec_driver_sql(
                    f'ALTER TABLE sdd_catalog."{table.name}" SET SCHEMA public'
                )
        for name in GUARD_FUNCTIONS:
            connection.exec_driver_sql(f"ALTER FUNCTION sdd_catalog.{name}() SET SCHEMA public")
        connection.exec_driver_sql("ALTER TABLE sdd_catalog.sdd_schema_version SET SCHEMA public")
        connection.execute(
            text("UPDATE public.sdd_schema_version SET version=:version"), {"version": version}
        )
        connection.exec_driver_sql("DROP SCHEMA sdd_catalog")


def catalog_identity(engine, namespace):
    with engine.connect() as connection:
        return (
            connection.execute(
                text(
                    "SELECT c.oid,c.relname,c.relowner,c.relacl::text FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname=:schema AND c.relkind IN ('r','i','S') ORDER BY c.relname"
                ),
                {"schema": namespace},
            ).all(),
            connection.execute(
                text(
                    "SELECT p.oid,p.proname,p.proowner,p.proacl::text FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid=p.pronamespace "
                    "WHERE n.nspname=:schema ORDER BY p.proname"
                ),
                {"schema": namespace},
            ).all(),
            connection.execute(
                text(
                    "SELECT c.oid,c.conname,c.conrelid,c.confrelid,c.contype "
                    "FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace "
                    "WHERE n.nspname=:schema ORDER BY c.oid"
                ),
                {"schema": namespace},
            ).all(),
        )


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_upgrade_retains_data_identity_guards_and_history(target, version):
    url, engine, role, _ = target
    password = secrets.token_urlsafe(32)
    migrate(url, role, password)
    runtime = Database(url.set(username=role, password=password))
    try:
        ledger, catalog = Ledger(runtime), Catalog(runtime)
        evidence = ledger.ingest("a", "key", "保留记录", "c", "s", "p", "2026-01-01T00:00:00Z")
        dataset = catalog.create("a", "records", [{"id": 1, "amount": 12.5}], primary_key=["id"])
        history = QueryHistory(runtime)
        result = history.capture(
            "a",
            "reviewer",
            {"mode": "sql", "text": "SELECT * FROM records"},
            lambda: SQLService(runtime).execute("a", "SELECT * FROM records"),
        )
        legacy_layout(engine, version)
        before = catalog_identity(engine, "public")
        assert check_migration(url, role)["schema_version"] == version
        migrate(url, role)
        migrate(url, role)
        after = catalog_identity(engine, "sdd_catalog")
        assert set(before[0]) <= set(after[0])
        assert before[1] == after[1]
        assert set(before[2]) <= set(after[2])
        assert catalog_identity(engine, "public") == ([], [], [])
        assert check_migration(url, role)["catalog_schema"] == "sdd_catalog"
        assert ledger.get("a", schema.versions, evidence["id"]) == evidence
        assert ledger.list("b", schema.versions) == []
        assert catalog.get("a", dataset["id"]) == dataset
        assert (
            history.detail("a", "reviewer", result["history_id"])["output"]["result"]
            == result["result"]
        )
        assert SQLService(runtime).execute("a", "SELECT amount FROM records")["result"] == [
            {"amount": "12.5000000000"}
        ]
        with pytest.raises(DBAPIError), runtime.transaction("a") as connection:
            connection.execute(update(schema.versions).values(text="tamper"))
        with engine.connect() as connection:
            with pytest.raises(DBAPIError), connection.begin():
                connection.execute(update(schema.versions).values(text="tamper"))
    finally:
        runtime.engine.dispose()


def test_late_migration_failure_rolls_back_every_relocation(target, monkeypatch):
    url, engine, role, _ = target
    migrate(url, role, secrets.token_urlsafe(32))
    legacy_layout(engine, 2)
    before = public_contract(engine)

    def reject_worker(*_):
        raise ValueError("injected deployment failure")

    monkeypatch.setattr("sdd.bootstrap.grant_worker", reject_worker)
    with pytest.raises(ValueError, match="injected"):
        migrate(url, role, sql_interface=True)
    assert public_contract(engine) == before
    assert check_migration(url, role)["schema_version"] == 2
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT to_regnamespace('sdd_catalog') IS NULL"
        ).scalar_one()
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM pg_extension WHERE extname='jevsd_pg'"
            ).scalar_one()
            == 0
        )


def test_new_upgrade_tables_keep_the_original_catalog_owner(target):
    url, engine, role, parent = target
    migrate(url, role, secrets.token_urlsafe(32))
    legacy_layout(engine, 1)
    with engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE ROLE "{parent}"')
        for table in schema.metadata.sorted_tables:
            if table.name not in VERSION_TWO_TABLES | VERSION_FOUR_TABLES:
                connection.exec_driver_sql(f'ALTER TABLE public."{table.name}" OWNER TO "{parent}"')
        connection.exec_driver_sql(f'ALTER TABLE public.sdd_schema_version OWNER TO "{parent}"')
        for name in GUARD_FUNCTIONS:
            connection.exec_driver_sql(f'ALTER FUNCTION public.{name}() OWNER TO "{parent}"')
    migrate(url, role)
    migrate(url, role)
    assert check_migration(url, role)["schema_version"] == SCHEMA_VERSION
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT DISTINCT pg_get_userbyid(c.relowner) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='sdd_catalog' AND c.relkind='r'"
        ).scalars().all() == [parent]


@pytest.mark.parametrize("legacy", [False, True])
def test_schema_default_grants_cannot_leave_an_unsafe_installation(target, legacy):
    url, engine, role, _ = target
    if legacy:
        migrate(url, role, secrets.token_urlsafe(32))
        legacy_layout(engine, 2)
    before = public_contract(engine)
    with engine.begin() as connection:
        if not legacy:
            connection.exec_driver_sql(f'CREATE ROLE "{role}" LOGIN')
        connection.exec_driver_sql(f'ALTER DEFAULT PRIVILEGES GRANT CREATE ON SCHEMAS TO "{role}"')
    with pytest.raises(InstallationConflict, match="default or inherited"):
        migrate(url, role)
    assert public_contract(engine) == before
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT to_regnamespace('sdd_catalog') IS NULL"
        ).scalar_one()
        assert (
            connection.exec_driver_sql(
                "SELECT to_regnamespace('sdd_data') IS NOT NULL"
            ).scalar_one()
            == legacy
        )


@pytest.mark.parametrize("change", ["ambiguous", "schema_owner", "runtime_create"])
def test_catalog_ownership_and_location_are_checked(target, change):
    url, engine, role, parent = target
    migrate(url, role, secrets.token_urlsafe(32))
    with engine.begin() as connection:
        if change == "ambiguous":
            connection.exec_driver_sql("CREATE TABLE public.sdd_schema_version(version integer)")
        elif change == "schema_owner":
            connection.exec_driver_sql(f'CREATE ROLE "{parent}"')
            connection.exec_driver_sql(f'ALTER SCHEMA sdd_catalog OWNER TO "{parent}"')
        else:
            connection.exec_driver_sql(f'GRANT CREATE ON SCHEMA sdd_catalog TO "{role}"')
    with pytest.raises(InstallationConflict):
        check_migration(url, role)
    with pytest.raises(InstallationConflict):
        migrate(url, role)
