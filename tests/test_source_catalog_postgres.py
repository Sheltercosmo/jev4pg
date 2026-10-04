"""Existing-relation adoption against an isolated PostgreSQL installation."""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlglot.errors import SqlglotError

from test_deployment_postgres import installation as installation
from sdd.bootstrap import SCHEMA_VERSION, migrate
from sdd.deployment import check_database
from sdd.generic.catalog import Catalog
from sdd.generic.hybrid import context_packet
from sdd.generic.sql import SQLService
from sdd.generic.source_catalog import lock_sources, validate_sources, source_transaction


pytestmark = pytest.mark.skipif(
    not os.getenv("SDD_TEST_ADMIN_URL"), reason="Dedicated admin test URL required"
)


@pytest.fixture
def source(installation):
    import uuid

    env = installation
    name = "source_" + uuid.uuid4().hex[:12]
    role = env["roles"][0]
    with env["owner"].engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{name}"')
        connection.exec_driver_sql(f'GRANT USAGE ON SCHEMA "{name}" TO "{role}"')
        connection.exec_driver_sql(f'''CREATE TABLE "{name}".records (
            id bigint PRIMARY KEY, tenant text NOT NULL, note text, amount numeric(38,10), hidden text
        )''')
        connection.exec_driver_sql(f'''INSERT INTO "{name}".records VALUES
            (1,'tenant-a','完成',12.30,'private'),(2,'tenant-a','pending',5.25,'private'),
            (3,'tenant-b','完成',99,'private')''')
        connection.exec_driver_sql(f'ALTER TABLE "{name}".records ENABLE ROW LEVEL SECURITY')
        connection.exec_driver_sql(f'''CREATE POLICY scope ON "{name}".records
            USING (tenant=current_setting('sdd.tenant',true))''')
        connection.exec_driver_sql(f'''COMMENT ON TABLE "{name}".records IS 'Work descriptions' ''')
        connection.exec_driver_sql(f'''COMMENT ON COLUMN "{name}".records.note IS '任务说明' ''')
        connection.exec_driver_sql(f'GRANT SELECT ON "{name}".records TO "{role}"')
    yield {
        **env,
        "schema": name,
        "catalog": Catalog(env["app"]),
        "service": SQLService(env["app"]),
    }
    catalog = Catalog(env["app"])
    for tenant in ("tenant-a", "tenant-b"):
        for dataset in catalog.list(tenant):
            if dataset["schema_name"] == name:
                catalog.detach(tenant, dataset["id"])


def attach(source, name="events", columns=None):
    return source["catalog"].attach("tenant-a", name, source["schema"], "records", columns)


def test_attachment_reads_live_source_without_copy_or_writes(source):
    catalog, sql, name = source["catalog"], source["service"], source["schema"]
    with source["owner"].engine.connect() as connection:
        before = connection.execute(
            text("SELECT count(*) FROM pg_class WHERE relnamespace='sdd_data'::regnamespace")
        ).scalar_one()
    dataset = attach(source, columns=["id", "note", "amount"])
    assert dataset["primary_key"] == ["id"] and not dataset["writable"]
    assert dataset["description"] == "Work descriptions"
    assert dataset["columns"][1]["description"] == "任务说明"
    assert dataset["columns"][2]["database_type"] == "numeric(38,10)"
    assert sql.execute("tenant-a", "SELECT SUM(amount) AS total FROM events")["result"] == [
        {"total": "17.5500000000"}
    ]
    assert set(catalog.rows("tenant-a", dataset, limit=1)[0]) == {"id", "note", "amount"}
    with pytest.raises((ValueError, SqlglotError)):
        sql.execute("tenant-a", "SELECT hidden FROM events")
    with pytest.raises(ValueError, match="read-only"):
        sql.execute("tenant-a", "UPDATE events SET amount=0")
    with pytest.raises(ValueError, match="unauthorized"):
        sql.execute("tenant-b", "SELECT * FROM events")
    with source["owner"].engine.begin() as connection:
        connection.exec_driver_sql(f'UPDATE "{name}".records SET amount=20 WHERE id=1')
        assert (
            connection.execute(
                text("SELECT count(*) FROM pg_class WHERE relnamespace='sdd_data'::regnamespace")
            ).scalar_one()
            == before
        )
    assert sql.execute("tenant-a", "SELECT SUM(amount) AS total FROM events")["result"] == [
        {"total": "25.2500000000"}
    ]


def test_attachment_keeps_rls_for_each_tenant_and_invoker_view(source):
    catalog, sql, name, role = (
        source["catalog"],
        source["service"],
        source["schema"],
        source["roles"][0],
    )
    attach(source)
    catalog.attach("tenant-b", "events", name, "records")
    assert sql.execute("tenant-b", "SELECT id FROM events")["result"] == [{"id": 3}]
    with source["owner"].engine.begin() as connection:
        connection.exec_driver_sql(
            f'CREATE VIEW "{name}".unsafe AS SELECT id,note FROM "{name}".records'
        )
        connection.exec_driver_sql(
            f'CREATE VIEW "{name}".safe WITH (security_invoker=true) AS SELECT id,note FROM "{name}".records'
        )
        connection.exec_driver_sql(f'GRANT SELECT ON "{name}".safe,"{name}".unsafe TO "{role}"')
    with pytest.raises(ValueError, match="security_invoker"):
        catalog.attach("tenant-a", "bad_view", name, "unsafe")
    catalog.attach("tenant-a", "视图", name, "safe")
    assert sql.execute("tenant-a", 'SELECT COUNT(*) AS n FROM "视图"')["result"] == [{"n": 2}]
    with source["owner"].engine.begin() as connection:
        connection.exec_driver_sql(
            f'CREATE OR REPLACE VIEW "{name}".safe WITH (security_invoker=true) AS SELECT id,note FROM "{name}".records WHERE id=1'
        )
    with pytest.raises(ValueError, match="schema changed"):
        sql.execute("tenant-a", 'SELECT * FROM "视图"')


@pytest.mark.parametrize("change", ["type", "replace", "key", "comment"])
def test_changed_source_identity_or_contract_stops_execution(source, change):
    dataset = attach(source)
    name = source["schema"]
    with source["owner"].engine.begin() as connection:
        if change == "type":
            connection.exec_driver_sql(
                f'ALTER TABLE "{name}".records ALTER COLUMN amount TYPE numeric(38,5)'
            )
        elif change == "key":
            connection.exec_driver_sql(f'ALTER TABLE "{name}".records DROP CONSTRAINT records_pkey')
        elif change == "comment":
            connection.exec_driver_sql(
                f'''COMMENT ON COLUMN "{name}".records.note IS 'Changed meaning' '''
            )
        else:
            connection.exec_driver_sql(f'ALTER TABLE "{name}".records RENAME TO old_records')
            connection.exec_driver_sql(
                f'CREATE TABLE "{name}".records (LIKE "{name}".old_records INCLUDING ALL)'
            )
            connection.exec_driver_sql(
                f'GRANT SELECT ON "{name}".records TO "{source["roles"][0]}"'
            )
    with pytest.raises(ValueError, match="schema changed"):
        source["service"].execute("tenant-a", "SELECT id,amount FROM events")
    with pytest.raises(ValueError, match="schema changed"):
        source["catalog"].model_catalog("tenant-a", [dataset["id"]], include_values=False)


def test_source_locks_pin_schema_before_repeatable_read(source):
    dataset = attach(source)
    with source["app"].transaction(
        "tenant-a",
        isolation_level="REPEATABLE READ",
        before_snapshot=lambda connection: lock_sources(connection, [dataset]),
    ) as connection:
        validate_sources(connection, [dataset])
        with pytest.raises(DBAPIError) as error:
            with source["owner"].engine.begin() as other:
                other.exec_driver_sql("SET LOCAL lock_timeout='100ms'")
                other.exec_driver_sql(
                    f'ALTER TABLE "{source["schema"]}".records ADD COLUMN changed int'
                )
        assert error.value.orig.sqlstate == "55P03"
        assert (
            connection.execute(
                text(f'SELECT count(*) FROM "{source["schema"]}".records')
            ).scalar_one()
            == 2
        )


def test_source_lock_does_not_establish_the_data_snapshot(source):
    name = source["schema"]
    dataset = attach(source)

    def before(connection):
        lock_sources(connection, [dataset])
        with source["owner"].engine.begin() as other:
            other.exec_driver_sql(f'UPDATE "{name}".records SET amount=100 WHERE id=1')

    with source["app"].transaction(
        "tenant-a", isolation_level="REPEATABLE READ", before_snapshot=before
    ) as connection:
        assert (
            str(
                connection.execute(
                    text(f'SELECT amount FROM "{name}".records WHERE id=1')
                ).scalar_one()
            )
            == "100.0000000000"
        )


def test_detach_preserves_relation_and_invalidates_old_binding(source):
    dataset = attach(source)
    result = source["catalog"].detach("tenant-a", dataset["id"])
    assert result["source_preserved"]
    with pytest.raises(ValueError):
        source["catalog"].get("tenant-a", dataset["id"])
    with source["app"].transaction("tenant-a") as connection:
        with pytest.raises(ValueError, match="attachment changed"):
            validate_sources(connection, [dataset])
        assert (
            connection.execute(
                text(f'SELECT count(*) FROM "{source["schema"]}".records')
            ).scalar_one()
            == 2
        )
    replacement = attach(source)
    assert replacement["id"] != dataset["id"]


def test_source_privileges_are_required_and_internal_tables_are_rejected(source):
    role, name = source["roles"][0], source["schema"]
    with source["owner"].engine.begin() as connection:
        connection.exec_driver_sql(f'REVOKE SELECT ON "{name}".records FROM "{role}"')
    with pytest.raises(DBAPIError):
        attach(source)
    with pytest.raises(ValueError, match="metadata"):
        source["catalog"].attach("tenant-a", "metadata", "public", "dataset_catalog")
    with pytest.raises(ValueError, match="internal"):
        source["catalog"].attach("tenant-a", "system", "pg_catalog", "pg_class")


def test_composite_foreign_keys_reach_planning_without_partial_join_rules(source):
    name, role, catalog = source["schema"], source["roles"][0], source["catalog"]
    with source["owner"].engine.begin() as connection:
        connection.exec_driver_sql(
            f'CREATE TABLE "{name}".parent (region text,code bigint,label text,PRIMARY KEY(region,code))'
        )
        connection.exec_driver_sql(
            f'CREATE TABLE "{name}".child (id bigint PRIMARY KEY,region text,code bigint,FOREIGN KEY(region,code) REFERENCES "{name}".parent(region,code))'
        )
        connection.exec_driver_sql(f'GRANT SELECT ON "{name}".parent,"{name}".child TO "{role}"')
    parent = catalog.attach("tenant-a", "组织", name, "parent")
    child = catalog.attach("tenant-a", "明细", name, "child")
    assert child["links"] == []
    relation = child["source_relationships"][0]
    assert relation["source_columns"] == ["region", "code"]
    assert relation["target_columns"] == ["region", "code"]
    packet = context_packet("哪些组织有记录？", [parent, child], [])
    assert packet["catalog"][1]["source_relationships"] == [relation]


def test_upgrade_requires_migration_and_preserves_existing_catalog(source):
    dataset = attach(source)
    with source["owner"].engine.begin() as connection:
        connection.exec_driver_sql("UPDATE sdd_schema_version SET version=1")
    with pytest.raises(ValueError, match="migration"):
        check_database(source["app"])
    migrate(source["url"], source["roles"][0], sql_interface=True)
    assert check_database(source["app"])["schema_version"] == SCHEMA_VERSION
    assert (
        source["catalog"].get("tenant-a", dataset["id"])["source_binding"]
        == dataset["source_binding"]
    )


@pytest.mark.parametrize("attached_side", ["source", "target"])
def test_manual_relationship_with_attachment_does_not_change_source_constraints(
    source, attached_side
):
    catalog = source["catalog"]
    external = attach(source, columns=["id"])
    imported = catalog.create(
        "tenant-a", "local_" + source["schema"], [{"id": 1}, {"id": 2}], primary_key=["id"]
    )
    left, right = (external, imported) if attached_side == "source" else (imported, external)
    with source["owner"].engine.connect() as connection:
        before = connection.execute(
            text("SELECT count(*) FROM pg_constraint WHERE contype='f'")
        ).scalar_one()
    linked = catalog.link("tenant-a", left["id"], right["id"], "id", "id")
    assert linked["links"] == [
        {
            "target_id": right["id"],
            "source_column": "id",
            "target_column": "id",
            "cardinality": "many_to_one",
        }
    ]
    with source["owner"].engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM pg_constraint WHERE contype='f'")
            ).scalar_one()
            == before
        )
    assert source["service"].execute("tenant-a", "SELECT COUNT(*) AS n FROM events")["result"] == [
        {"n": 2}
    ]


@pytest.mark.parametrize("kind", ["partitioned", "materialized"])
def test_existing_relation_kinds_and_unkeyed_duplicate_rows(source, kind):
    name, role, catalog = source["schema"], source["roles"][0], source["catalog"]
    with source["owner"].engine.begin() as connection:
        if kind == "partitioned":
            connection.exec_driver_sql(
                f'CREATE TABLE "{name}".inputs(note text,p int) PARTITION BY RANGE(p)'
            )
            connection.exec_driver_sql(
                f'CREATE TABLE "{name}".part PARTITION OF "{name}".inputs DEFAULT'
            )
            connection.exec_driver_sql(
                f"INSERT INTO \"{name}\".inputs VALUES ('same',1),('same',1),(NULL,2)"
            )
        else:
            connection.exec_driver_sql(
                f"CREATE MATERIALIZED VIEW \"{name}\".inputs AS SELECT * FROM (VALUES ('same',1),('same',1),(NULL,2)) s(note,p)"
            )
        connection.exec_driver_sql(f'GRANT SELECT ON "{name}".inputs TO "{role}"')
    dataset = catalog.attach("tenant-a", "重复记录", name, "inputs")
    assert dataset["primary_key"] == []
    result = source["service"].execute(
        "tenant-a",
        'SELECT note,COUNT(*) AS n FROM "重复记录" GROUP BY note ORDER BY note NULLS LAST',
    )
    assert result["result"] == [{"note": "same", "n": 2}, {"note": None, "n": 1}]


def test_large_relation_is_queried_in_place_and_unsupported_columns_are_explicit(source):
    name, role, catalog = source["schema"], source["roles"][0], source["catalog"]
    with source["owner"].engine.begin() as connection:
        connection.exec_driver_sql(
            f'CREATE TABLE "{name}".large AS SELECT n AS id,ARRAY[n] AS items FROM generate_series(1,60001) n'
        )
        connection.exec_driver_sql(f'GRANT SELECT ON "{name}".large TO "{role}"')
    with pytest.raises(ValueError, match="Unsupported source type"):
        catalog.attach("tenant-a", "large", name, "large")
    catalog.attach("tenant-a", "large", name, "large", ["id"])

    def no_population(*args, **kwargs):
        raise AssertionError("An attached SQL read must not materialize source rows in Python")

    source["service"].snapshots = no_population
    assert source["service"].execute("tenant-a", "SELECT COUNT(*) AS n FROM large")["result"] == [
        {"n": 60001}
    ]


def test_quoted_identifiers_uuid_and_physical_types(source):
    name, role, catalog = source["schema"], source["roles"][0], source["catalog"]
    with source["owner"].engine.begin() as connection:
        connection.exec_driver_sql(
            f'''CREATE TABLE "{name}"."Mixed ' records" ("编号" uuid PRIMARY KEY,"说明" text)'''
        )
        connection.exec_driver_sql(
            f'''INSERT INTO "{name}"."Mixed ' records" VALUES ('00000000-0000-0000-0000-000000000001','完成')'''
        )
        connection.exec_driver_sql(f'''GRANT SELECT ON "{name}"."Mixed ' records" TO "{role}"''')
    dataset = catalog.attach("tenant-a", "事项", name, "Mixed ' records")
    assert dataset["columns"][0]["database_type"] == "uuid"
    assert source["service"].execute(
        "tenant-a", """SELECT "编号" FROM "事项" WHERE "说明"='完成' """
    )["result"] == [{"编号": "00000000-0000-0000-0000-000000000001"}]


def test_indirect_source_guard_blocks_refresh_and_releases_after_error(source):
    name, role = source["schema"], source["roles"][0]
    with source["owner"].engine.begin() as connection:
        connection.exec_driver_sql(
            f'CREATE MATERIALIZED VIEW "{name}".snapshot AS SELECT id,note FROM "{name}".records'
        )
        connection.exec_driver_sql(f'GRANT SELECT ON "{name}".snapshot TO "{role}"')
    dataset = source["catalog"].attach("tenant-a", "snapshot", name, "snapshot")
    with pytest.raises(ValueError, match="cancel fixture"):
        with source_transaction(source["app"], "tenant-a", [dataset], "REPEATABLE READ"):
            with pytest.raises(DBAPIError) as error:
                with source["owner"].engine.begin() as other:
                    other.exec_driver_sql("SET LOCAL lock_timeout='100ms'")
                    other.exec_driver_sql(f'REFRESH MATERIALIZED VIEW "{name}".snapshot')
            assert error.value.orig.sqlstate == "55P03"
            raise ValueError("cancel fixture")
    with source["owner"].engine.begin() as connection:
        connection.exec_driver_sql("SET LOCAL lock_timeout='1000ms'")
        connection.exec_driver_sql(f'REFRESH MATERIALIZED VIEW "{name}".snapshot')


def test_nested_views_require_invoker_security_and_definition_identity(source):
    name, role = source["schema"], source["roles"][0]
    with source["owner"].engine.begin() as connection:
        connection.exec_driver_sql(
            f'CREATE VIEW "{name}".inner_view AS SELECT id,note FROM "{name}".records'
        )
        connection.exec_driver_sql(
            f'CREATE VIEW "{name}".outer_view WITH (security_invoker=true) AS SELECT * FROM "{name}".inner_view'
        )
        connection.exec_driver_sql(
            f'GRANT SELECT ON "{name}".inner_view,"{name}".outer_view TO "{role}"'
        )
    with pytest.raises(ValueError, match="security_invoker"):
        source["catalog"].attach("tenant-a", "nested", name, "outer_view")
    with source["owner"].engine.begin() as connection:
        connection.exec_driver_sql(f'ALTER VIEW "{name}".inner_view SET (security_invoker=true)')
    dataset = source["catalog"].attach("tenant-a", "nested", name, "outer_view")
    assert "SELECT" not in str(dataset["source_binding"])
    assert source["service"].execute("tenant-a", "SELECT COUNT(*) AS n FROM nested")["result"] == [
        {"n": 2}
    ]
    with source["owner"].engine.begin() as connection:
        connection.exec_driver_sql(
            f'CREATE OR REPLACE VIEW "{name}".inner_view WITH (security_invoker=true) AS SELECT id,note FROM "{name}".records WHERE id=1'
        )
    with pytest.raises(ValueError, match="schema changed"):
        source["service"].execute("tenant-a", "SELECT * FROM nested")


def test_operator_cli_attaches_and_detaches_without_model_configuration(source):
    import json
    import subprocess
    import sys

    environment = {
        **os.environ,
        "DATABASE_URL": source["app"].engine.url.render_as_string(hide_password=False),
    }

    def command(*arguments):
        result = subprocess.run(
            [sys.executable, "-m", "sdd.cli", *arguments],
            env=environment,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    dataset = command(
        "attach",
        "cli_events",
        "--tenant",
        "tenant-a",
        "--schema",
        source["schema"],
        "--table",
        "records",
        "--column",
        "id",
        "--column",
        "note",
    )
    assert [column["name"] for column in dataset["columns"]] == ["id", "note"]
    assert command("detach", "cli_events", "--tenant", "tenant-a")["source_preserved"]
