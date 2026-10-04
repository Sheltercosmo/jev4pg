"""Source adoption contracts on real PostgreSQL, without inference."""

import copy
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.engine import make_url

from sdd.generic import schema
from sdd.generic.source_manifest import SourceOnboarding
from sdd.bootstrap import ensure_login, migrate
from sdd.db import Database
from sdd.generic.catalog import Catalog
from sdd.generic.sql import SQLService
from test_deployment_postgres import installation as installation
from test_source_catalog_postgres import source as source

pytestmark = pytest.mark.skipif(
    not os.getenv("SDD_TEST_ADMIN_URL"), reason="Admin test URL required"
)


def manifest(source, *names, columns=None, rebind=False):
    return {
        "version": 1,
        "sources": [
            {
                "name": name,
                "schema": source["schema"],
                "table": "records",
                "columns": columns or ["id", "note", "amount"],
                "rebind": rebind,
            }
            for name in names
        ],
    }


@pytest.mark.parametrize("name", ["activity", "事项", "Orders Renamed"])
def test_preview_apply_repeat_and_tenant_rows(source, name):
    catalog = source["catalog"]
    onboarding = SourceOnboarding(catalog)
    before = catalog.list("tenant-a")
    plan = onboarding.preview("tenant-a", manifest(source, name))
    assert catalog.list("tenant-a") == before
    assert plan["sources"][0]["definition"]["primary_key"] == ["id"]
    assert plan["sources"][0]["action"] == "create"
    first = onboarding.apply("tenant-a", json.loads(json.dumps(plan)))
    repeated = onboarding.apply("tenant-a", plan)
    assert first["created"] == repeated["retained"] and repeated["created"] == []
    assert onboarding.preview("tenant-a", manifest(source, name))["sources"][0]["action"] == "keep"
    rows = catalog.rows("tenant-a", catalog.get("tenant-a", name))
    assert len(rows) == 2 and {row["id"] for row in rows} == {1, 2}
    with pytest.raises(ValueError, match="different tenant"):
        onboarding.apply("tenant-b", plan)


def test_preview_and_apply_do_not_evaluate_source_rows(source):
    name, role = source["schema"], source["roles"][0]
    with source["owner"].engine.begin() as connection:
        connection.exec_driver_sql(f'''CREATE FUNCTION "{name}".row_probe(text) RETURNS text
            LANGUAGE plpgsql VOLATILE AS $$BEGIN RAISE EXCEPTION 'row evaluated'; END$$''')
        connection.exec_driver_sql(f'''CREATE VIEW "{name}".guarded WITH(security_invoker=true) AS
            SELECT id,"{name}".row_probe(note) AS note FROM "{name}".records''')
        connection.exec_driver_sql(f'GRANT SELECT ON "{name}".guarded TO "{role}"')
    spec = manifest(source, "guarded", columns=["id", "note"])
    spec["sources"][0]["table"] = "guarded"
    onboarding = SourceOnboarding(source["catalog"])
    assert onboarding.apply("tenant-a", onboarding.preview("tenant-a", spec))["created"]


@pytest.mark.parametrize("unsupported", [{"where": {"tenant": "a"}}, {"primary_key": ["note"]}])
def test_manifest_does_not_silently_accept_filters_or_invented_keys(source, unsupported):
    spec = manifest(source, "entries")
    spec["sources"][0].update(unsupported)
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        SourceOnboarding(source["catalog"]).preview("tenant-a", spec)


def test_composite_relationships_are_reviewed_in_full(source):
    name, role = source["schema"], source["roles"][0]
    with source["owner"].engine.begin() as connection:
        connection.exec_driver_sql(
            f'CREATE TABLE "{name}".parent (a integer,b text,PRIMARY KEY(a,b))'
        )
        connection.exec_driver_sql(f'''CREATE TABLE "{name}".child (id integer,a integer,b text,
            FOREIGN KEY(a,b) REFERENCES "{name}".parent(a,b))''')
        connection.exec_driver_sql(f'GRANT SELECT ON "{name}".parent,"{name}".child TO "{role}"')
    spec = {
        "version": 1,
        "sources": [
            {"name": "父记录", "schema": name, "table": "parent"},
            {"name": "子记录", "schema": name, "table": "child"},
        ],
    }
    onboarding = SourceOnboarding(source["catalog"])
    plan = onboarding.preview("tenant-a", spec)
    relationship = plan["relationships"][0]
    assert relationship["source_columns"] == relationship["target_columns"] == ["a", "b"]
    onboarding.apply("tenant-a", plan)
    assert source["catalog"].get("tenant-a", "子记录")["source_relationships"][0][
        "target_columns"
    ] == ["a", "b"]


def test_late_write_failure_rolls_back_all_sources(source, monkeypatch):
    from sdd.generic import source_manifest

    onboarding = SourceOnboarding(source["catalog"])
    plan = onboarding.preview("tenant-a", manifest(source, "first", "second"))
    original = source_manifest.insert_attachment
    calls = []

    def fail_second(*args):
        calls.append(args[2])
        if len(calls) == 2:
            raise RuntimeError("late write failure")
        original(*args)

    before = source["catalog"].list("tenant-a")
    monkeypatch.setattr(source_manifest, "insert_attachment", fail_second)
    with pytest.raises(RuntimeError, match="late write failure"):
        onboarding.apply("tenant-a", plan)
    assert len(calls) == 2 and source["catalog"].list("tenant-a") == before


def test_schema_drift_requires_new_review_then_explicit_rebind(source):
    onboarding = SourceOnboarding(source["catalog"])
    spec = manifest(source, "entries")
    plan = onboarding.preview("tenant-a", spec)
    original = onboarding.apply("tenant-a", plan)["created"][0]
    with source["owner"].engine.begin() as connection:
        connection.exec_driver_sql(
            f"COMMENT ON COLUMN \"{source['schema']}\".records.note IS '更新后的说明'"
        )
    with pytest.raises(ValueError, match="changed after preview"):
        onboarding.apply("tenant-a", plan)
    conflict = onboarding.preview("tenant-a", spec)
    assert conflict["sources"][0]["action"] == "conflict"
    with pytest.raises(ValueError, match="Resolve source conflicts"):
        onboarding.apply("tenant-a", conflict)
    replacement = onboarding.preview("tenant-a", manifest(source, "entries", rebind=True))
    assert replacement["sources"][0]["action"] == "rebind"
    new_id = onboarding.apply("tenant-a", replacement)["created"][0]
    assert new_id != original
    assert onboarding.apply("tenant-a", replacement)["retained"] == [new_id]
    with source["app"].transaction("tenant-a") as connection:
        old = (
            connection.execute(select(schema.datasets).where(schema.datasets.c.id == original))
            .mappings()
            .one()
        )
        assert old["name"] == "_sdd_detached_" + original


def test_catalog_replacement_and_edited_plan_are_rejected(source):
    onboarding = SourceOnboarding(source["catalog"])
    plan = onboarding.preview("tenant-a", manifest(source, "entries"))
    edited = copy.deepcopy(plan)
    edited["sources"][0]["description"] = "silently changed"
    with pytest.raises(ValueError, match="edited"):
        onboarding.apply("tenant-a", edited)
    source["catalog"].attach(
        "tenant-a", "entries", source["schema"], "records", ["id", "note", "amount"]
    )
    with pytest.raises(ValueError, match="Catalog entry"):
        onboarding.apply("tenant-a", plan)


def test_concurrent_same_plan_has_one_publication(source):
    onboarding = SourceOnboarding(source["catalog"])
    plan = onboarding.preview("tenant-a", manifest(source, "one", "two"))
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: onboarding.apply("tenant-a", plan), range(2)))
    assert sorted(len(result["created"]) for result in results) == [0, 2]
    assert sorted(len(result["retained"]) for result in results) == [0, 2]


def test_imported_data_is_never_replaced_by_a_manifest(source):
    catalog = source["catalog"]
    name = "imported_" + source["schema"]
    imported = catalog.create("tenant-a", name, [{"id": 1, "note": "keep"}])
    original_rows = catalog.rows("tenant-a", imported)
    onboarding = SourceOnboarding(catalog)
    plan = onboarding.preview("tenant-a", manifest(source, name, rebind=True))
    assert plan["sources"][0]["action"] == "conflict"
    with pytest.raises(ValueError, match="Resolve source conflicts"):
        onboarding.apply("tenant-a", plan)
    assert catalog.rows("tenant-a", imported) == original_rows


def test_unkeyed_duplicates_and_nulls_are_preserved(source):
    name, role = source["schema"], source["roles"][0]
    with source["owner"].engine.begin() as connection:
        connection.exec_driver_sql(
            f'CREATE TABLE "{name}".unkeyed (note text, amount numeric(12,3))'
        )
        connection.exec_driver_sql(
            f'''INSERT INTO "{name}".unkeyed VALUES (NULL,1.250),(NULL,1.250),('重复',NULL)'''
        )
        connection.exec_driver_sql(f'GRANT SELECT ON "{name}".unkeyed TO "{role}"')
    spec = {"version": 1, "sources": [{"name": "未设主键", "schema": name, "table": "unkeyed"}]}
    onboarding = SourceOnboarding(source["catalog"])
    plan = onboarding.preview("tenant-a", spec)
    assert plan["sources"][0]["definition"]["primary_key"] == []
    onboarding.apply("tenant-a", plan)
    rows = source["service"].execute("tenant-a", 'SELECT * FROM "未设主键"')["result"]
    assert rows.count({"note": None, "amount": "1.250"}) == 2
    assert {"note": "重复", "amount": None} in rows


@pytest.mark.parametrize("field", [None, "cluster", "database", "role"])
def test_missing_or_different_origin_blocks_even_matching_relation_oid(source, field):
    catalog = source["catalog"]
    dataset = catalog.attach("tenant-a", "restored", source["schema"], "records")
    definition = copy.deepcopy(dataset["source_binding"])
    if field is None:
        definition.pop("database_identity")
    else:
        previous = definition["database_identity"][field]
        definition["database_identity"][field] = (
            previous + "-changed" if isinstance(previous, str) else previous + 1
        )
    with source["app"].transaction("tenant-a") as connection:
        connection.execute(
            update(schema.source_bindings)
            .where(schema.source_bindings.c.dataset_id == dataset["id"])
            .values(definition=definition)
        )
    with pytest.raises(ValueError, match="database identity"):
        source["service"].execute("tenant-a", "SELECT * FROM restored")
    onboarding = SourceOnboarding(catalog)
    plan = onboarding.preview("tenant-a", manifest(source, "restored", rebind=True))
    assert onboarding.apply("tenant-a", plan)["created"] != [dataset["id"]]


def test_cli_review_artifact_can_be_applied_and_cannot_be_overwritten(source, tmp_path):
    path, output = tmp_path / "sources.json", tmp_path / "review.json"
    path.write_text(json.dumps(manifest(source, "记录")), encoding="utf-8")
    environment = {
        **os.environ,
        "DATABASE_URL": source["app"].engine.url.render_as_string(hide_password=False),
    }
    environment.pop("DATABASE_URL_FILE", None)
    command = [sys.executable, "-m", "sdd.cli", "sources"]
    preview = [*command, "preview", str(path), "--tenant", "tenant-a", "--output", str(output)]
    result = subprocess.run(preview, env=environment, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["actions"][0]["action"] == "create"
    before = output.read_bytes()
    assert subprocess.run(preview, env=environment, capture_output=True).returncode == 1
    assert output.read_bytes() == before
    result = subprocess.run(
        [*command, "apply", str(output), "--tenant", "tenant-a"],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout)["created"]
    changed = manifest(source, "记录")
    changed["sources"][0]["description"] = "需要重新审阅的说明"
    path.write_text(json.dumps(changed), encoding="utf-8")
    conflict_path = tmp_path / "conflict.json"
    result = subprocess.run(
        [*command, "preview", str(path), "--tenant", "tenant-a", "--output", str(conflict_path)],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1 and json.loads(result.stdout)["status"] == "blocked"
    assert (
        json.loads(conflict_path.read_text(encoding="utf-8"))["sources"][0]["action"] == "conflict"
    )


@pytest.mark.skipif(
    not os.getenv("SDD_TEST_PEER_ADMIN_URL") or not os.getenv("SDD_TEST_PG_BIN"),
    reason="Second disposable PostgreSQL cluster and client binaries required",
)
def test_dump_restore_requires_explicit_rebinding_on_another_cluster(source, tmp_path):
    peer_url = make_url(os.environ["SDD_TEST_PEER_ADMIN_URL"])
    database_name = source["url"].database
    target_url = peer_url.set(database=database_name)
    admin = create_engine(peer_url, isolation_level="AUTOCOMMIT")
    password = secrets.token_urlsafe(32)
    created_roles = []
    db = None
    logical_name = "restore_" + source["schema"]
    original = source["catalog"].attach(
        "tenant-a", logical_name, source["schema"], "records", ["id", "note"]
    )
    archive = tmp_path / "source-backup.dump"
    pg = Path(os.environ["SDD_TEST_PG_BIN"])
    suffix = ".exe" if os.name == "nt" else ""

    def client(binary, url, *arguments):
        environment = {**os.environ, "PGPASSWORD": url.password or ""}
        public_url = url.set(drivername="postgresql", password=None).render_as_string(
            hide_password=False
        )
        subprocess.run(
            [str(pg / (binary + suffix)), "--dbname=" + public_url, *arguments],
            env=environment,
            capture_output=True,
            check=True,
        )

    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
    try:
        with admin.begin() as connection:
            for role in source["roles"]:
                if ensure_login(connection, role, password):
                    created_roles.append(role)
        client("pg_dump", source["url"], "--format=custom", "--no-acl", "--file=" + str(archive))
        client("pg_restore", target_url, "--no-acl", "--exit-on-error", str(archive))
        migrate(target_url, source["roles"][0], sql_interface=True)
        owner = Database(target_url)
        try:
            with owner.engine.begin() as connection:
                name, role = source["schema"], source["roles"][0]
                connection.exec_driver_sql(f'GRANT USAGE ON SCHEMA "{name}" TO "{role}"')
                connection.exec_driver_sql(f'GRANT SELECT ON "{name}".records TO "{role}"')
        finally:
            owner.engine.dispose()
        db = Database(target_url.set(username=source["roles"][0], password=password))
        catalog = Catalog(db)
        restored = catalog.get("tenant-a", logical_name)
        assert restored["source_binding"] == original["source_binding"]
        with pytest.raises(ValueError, match="database identity"):
            SQLService(db).execute("tenant-a", f'SELECT * FROM "{logical_name}"')
        onboarding = SourceOnboarding(catalog)
        plan = onboarding.preview(
            "tenant-a", manifest(source, logical_name, columns=["id", "note"], rebind=True)
        )
        assert (
            plan["database_identity"]["cluster"]
            != original["source_binding"]["database_identity"]["cluster"]
        )
        with pytest.raises(ValueError, match="source database changed"):
            SourceOnboarding(source["catalog"]).apply("tenant-a", plan)
        replacement = onboarding.apply("tenant-a", plan)["created"][0]
        assert replacement != original["id"]
        assert SQLService(db).execute("tenant-a", f'SELECT id FROM "{logical_name}" ORDER BY id')[
            "result"
        ] == [{"id": 1}, {"id": 2}]
    finally:
        if db is not None:
            db.engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{database_name}" WITH (FORCE)')
            for role in created_roles:
                connection.exec_driver_sql(f'DROP ROLE "{role}"')
        admin.dispose()
