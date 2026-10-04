"""Reviewed writes on large managed tables, using an isolated PostgreSQL database."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
import os
from pathlib import Path
from statistics import median
from threading import Barrier, Event
from time import perf_counter
import tracemalloc

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from test_deployment_postgres import installation as installation
from sdd.generic import mutation, schema
from sdd.generic.catalog import Catalog
from sdd.generic.sql import SQLService


pytestmark = pytest.mark.skipif(
    not os.getenv("SDD_TEST_ADMIN_URL"), reason="Dedicated PostgreSQL server required"
)


@pytest.fixture(scope="module")
def writes(installation):
    db = installation["app"]
    catalog = Catalog(db)
    dataset = catalog.create(
        "tenant-a",
        "events",
        [],
        primary_key=["id"],
        columns=[
            {"name": "id", "type": "integer"},
            {"name": "amount", "type": "number"},
            {"name": "status", "type": "text"},
            {"name": "note", "type": "text"},
        ],
    )
    relation = f'sdd_data."{dataset["table_name"]}"'
    with installation["owner"].engine.begin() as conn:
        conn.exec_driver_sql(f"""INSERT INTO {relation}
            SELECT n, n::numeric/100, 'pending', 'record '||n FROM generate_series(1,1000000) n""")
        conn.exec_driver_sql(f"ANALYZE {relation}")
    return {
        **installation,
        "catalog": catalog,
        "dataset": dataset,
        "sql": SQLService(db),
        "relation": relation,
    }


def read(writes, identity):
    with writes["app"].transaction("tenant-a") as conn:
        return dict(
            conn.execute(text(f"SELECT * FROM {writes['relation']} WHERE id=:id"), {"id": identity})
            .mappings()
            .one()
        )


def test_indexed_update_without_table_copy(writes, monkeypatch):
    monkeypatch.setattr(Catalog, "rows", lambda *a, **kw: pytest.fail("Full table copy"))
    sql = writes["sql"]
    p = sql.execute(
        "tenant-a", "UPDATE events SET amount=amount+0.12345678901 WHERE id=950003", actor="r"
    )
    assert p["changes_sample"] == [{"amount": "9500.1534567890"}]
    assert p["manifest"]["source_rows_state"] == "NOT_EVALUATED"
    assert p["manifest"]["mutation_scope"] == "reviewed_targets"
    with writes["owner"].engine.begin() as conn:
        conn.exec_driver_sql(f"UPDATE {writes['relation']} SET note='unrelated' WHERE id=999999")
    result = sql.commit("tenant-a", p["preview_token"], "r")
    assert result["manifest"]["affected_rows"] == 1
    assert read(writes, 950003)["amount"] == Decimal("9500.1534567890")
    assert "950003" not in result["compiled_sql"]
    with pytest.raises(ValueError, match="invalid"):
        sql.commit("tenant-a", p["preview_token"], "r")


def test_stale_selected_rows_and_membership(writes):
    sql = writes["sql"]
    p = sql.execute(
        "tenant-a", "UPDATE events SET status='done' WHERE id BETWEEN 21 AND 22", actor="r"
    )
    with writes["owner"].engine.begin() as conn:
        conn.exec_driver_sql(f"UPDATE {writes['relation']} SET amount=0 WHERE id=21")
    with pytest.raises(ValueError, match="stale"):
        sql.commit("tenant-a", p["preview_token"], "r")
    p = sql.execute("tenant-a", "DELETE FROM events WHERE note='new match'", actor="r")
    with writes["owner"].engine.begin() as conn:
        conn.exec_driver_sql(f"UPDATE {writes['relation']} SET note='new match' WHERE id=23")
    with pytest.raises(ValueError, match="stale"):
        sql.commit("tenant-a", p["preview_token"], "r")
    assert read(writes, 22)["status"] == "pending"


def test_predicate_is_pinned_after_lock_without_blocking_unrelated_writes(writes, monkeypatch):
    sql = writes["sql"]
    p = sql.execute("tenant-a", "UPDATE events SET status='done' WHERE note='record 31'", actor="r")
    original = mutation._targets
    locked, resume = Event(), Event()

    def pause(*args, **kwargs):
        rows = original(*args, **kwargs)
        if kwargs["lock"]:
            locked.set()
            assert resume.wait(10)
        return rows

    monkeypatch.setattr(mutation, "_targets", pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(sql.commit, "tenant-a", p["preview_token"], "r")
        try:
            assert locked.wait(10)
            with writes["owner"].engine.begin() as conn:
                conn.exec_driver_sql("SET LOCAL lock_timeout='200ms'")
                conn.exec_driver_sql(
                    f"UPDATE {writes['relation']} SET note='record 31' WHERE id=32"
                )
            with pytest.raises(DBAPIError), writes["owner"].engine.begin() as conn:
                conn.exec_driver_sql("SET LOCAL lock_timeout='200ms'")
                conn.exec_driver_sql(f"UPDATE {writes['relation']} SET amount=0 WHERE id=31")
        finally:
            resume.set()
        assert future.result(timeout=10)["manifest"]["affected_rows"] == 1
    assert read(writes, 31)["status"] == "done"
    assert read(writes, 32)["status"] == "pending"


def test_insert_delete_and_rollback(writes, monkeypatch):
    monkeypatch.setattr(Catalog, "rows", lambda *a, **kw: pytest.fail("Full table copy"))
    sql = writes["sql"]
    p = sql.execute(
        "tenant-a", "INSERT INTO events (id,amount,status) VALUES (1000001,0.01,'新建')", actor="r"
    )
    with pytest.raises(ValueError):
        sql.commit("tenant-a", p["preview_token"], "other")
    sql.commit("tenant-a", p["preview_token"], "r")
    assert read(writes, 1000001)["status"] == "新建"
    duplicate = sql.execute(
        "tenant-a", "INSERT INTO events (id) VALUES (1000002),(1000001)", actor="r"
    )
    with pytest.raises(DBAPIError):
        sql.commit("tenant-a", duplicate["preview_token"], "r")
    with writes["app"].transaction("tenant-a") as conn:
        assert (
            conn.execute(
                text(f"SELECT count(*) FROM {writes['relation']} WHERE id=1000002")
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                schema.previews.select().where(schema.previews.c.id == duplicate["preview_token"])
            )
            .mappings()
            .one()["state"]
            == "pending"
        )
    deletion = sql.execute("tenant-a", "DELETE FROM events WHERE id=1000001", actor="r")
    assert sql.commit("tenant-a", deletion["preview_token"], "r")["result"] == [
        {"affected_rows": 1}
    ]


def test_budgets_and_empty_scope(writes):
    sql = writes["sql"]
    with pytest.raises(ValueError, match="budget"):
        sql.execute("tenant-a", "UPDATE events SET amount=1 WHERE id>0", max_affected=2)
    with pytest.raises(ValueError, match="allow_all"):
        sql.execute("tenant-a", "DELETE FROM events")
    p = sql.execute("tenant-a", "DELETE FROM events WHERE id<0", actor="r")
    assert p["affected_rows"] == 0
    assert sql.commit("tenant-a", p["preview_token"], "r")["result"] == [{"affected_rows": 0}]
    with pytest.raises(ValueError):
        sql.execute("tenant-b", "DELETE FROM events WHERE id=1")


def test_composite_keys_alias_chinese_nulls_and_assignment_rounding(writes):
    writes["catalog"].create(
        "tenant-a",
        "读数",
        [
            {"设备": "甲", "序号": 1, "读数": 2, "备注": None},
            {"设备": "乙", "序号": 1, "读数": 2, "备注": None},
        ],
        columns=[
            {"name": "设备", "type": "text"},
            {"name": "序号", "type": "integer"},
            {"name": "读数", "type": "integer"},
            {"name": "备注", "type": "text"},
        ],
        primary_key=["设备", "序号"],
    )
    sql = writes["sql"]
    p = sql.execute(
        "tenant-a",
        """UPDATE "读数" AS r SET "读数"=r."读数"*1.25, "备注"='已审核'
        WHERE r."设备"='甲' AND r."备注" IS NULL""",
        actor="r",
    )
    assert p["changes_sample"] == [{"读数": 3, "备注": "已审核"}]
    sql.commit("tenant-a", p["preview_token"], "r")
    rows = sql.execute("tenant-a", 'SELECT "设备","读数" FROM "读数" ORDER BY "读数"')["result"]
    assert rows == [{"设备": "乙", "读数": 2}, {"设备": "甲", "读数": 3}]


def test_million_row_bounded_write_measurement(writes):
    sql = writes["sql"]
    previews, commits = [], []
    for identity in range(800001, 800009):
        start = perf_counter()
        p = sql.execute(
            "tenant-a", f"UPDATE events SET amount=amount+1 WHERE id={identity}", actor="r"
        )
        previews.append((perf_counter() - start) * 1000)
        start = perf_counter()
        assert sql.commit("tenant-a", p["preview_token"], "r")["manifest"]["affected_rows"] == 1
        commits.append((perf_counter() - start) * 1000)
    tracemalloc.start()
    p = sql.execute(
        "tenant-a",
        "UPDATE events SET amount=amount+1 WHERE id BETWEEN 810001 AND 811000",
        actor="r",
    )
    sql.commit("tenant-a", p["preview_token"], "r")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 32 * 1024**2
    report = {
        "population": 1000000,
        "source": "generated",
        "repetitions": 8,
        "single_row_preview_median_ms": round(median(previews), 2),
        "single_row_commit_median_ms": round(median(commits), 2),
        "thousand_row_python_peak_mib": round(peak / 1024**2, 2),
        "provider_calls": 0,
    }
    Path(".runtime").mkdir(exist_ok=True)
    Path(".runtime/mutation-validation.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report))


def test_row_security_and_trigger_result_rollback(writes):
    dataset = writes["catalog"].create(
        "tenant-a", "secured", [{"id": 1, "value": 10}, {"id": 2, "value": 20}], primary_key=["id"]
    )
    relation = f'sdd_data."{dataset["table_name"]}"'
    with writes["owner"].engine.begin() as conn:
        conn.exec_driver_sql(
            f"CREATE POLICY restrict_rows ON {relation} AS RESTRICTIVE USING (id=1)"
        )
    sql = writes["sql"]
    preview = sql.execute("tenant-a", "UPDATE secured SET value=value+1 WHERE id>0", actor="r")
    assert preview["affected_rows"] == 1
    sql.commit("tenant-a", preview["preview_token"], "r")
    with writes["owner"].engine.begin() as conn:
        assert conn.exec_driver_sql(f"SELECT value FROM {relation} WHERE id=2").scalar_one() == 20
        conn.exec_driver_sql("""CREATE FUNCTION public.change_reviewed_value() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN NEW.value := NEW.value+100; RETURN NEW; END $$""")
        conn.exec_driver_sql(f"""CREATE TRIGGER change_value BEFORE UPDATE ON {relation}
            FOR EACH ROW EXECUTE FUNCTION public.change_reviewed_value()""")
    preview = sql.execute("tenant-a", "UPDATE secured SET value=12 WHERE id=1", actor="r")
    with pytest.raises(ValueError, match="differs from the reviewed"):
        sql.commit("tenant-a", preview["preview_token"], "r")
    assert sql.execute("tenant-a", "SELECT value FROM secured")["result"] == [{"value": 11}]


def test_review_memory_limit_and_expiration(writes):
    dataset = writes["catalog"].create(
        "tenant-a", "wide", [{"id": 1, "note": "x"}], primary_key=["id"]
    )
    with writes["owner"].engine.begin() as conn:
        conn.exec_driver_sql(
            f'''UPDATE sdd_data."{dataset["table_name"]}" SET note=repeat('x',4200000)'''
        )
    with pytest.raises(ValueError, match="4 MiB"):
        writes["sql"].execute("tenant-a", "DELETE FROM wide WHERE id=1")
    p = writes["sql"].execute("tenant-a", "UPDATE events SET amount=0 WHERE id=65", actor="r")
    with writes["app"].transaction("tenant-a") as conn:
        conn.execute(
            schema.previews.update()
            .where(schema.previews.c.id == p["preview_token"])
            .values(expires_at=0)
        )
    with pytest.raises(ValueError, match="expired"):
        writes["sql"].commit("tenant-a", p["preview_token"], "r")
    assert read(writes, 65)["amount"] == Decimal("0.65")


def test_delete_redacts_only_related_history_in_database(writes):
    from sdd.generic.history import QueryHistory
    from sdd.ledger import now, uid

    sql = writes["sql"]
    old = sql.execute("tenant-a", "SELECT * FROM events WHERE id=72")
    writes["catalog"].create("tenant-a", "unrelated_history", [{"id": 1}], primary_key=["id"])
    unaffected = sql.execute("tenant-a", "SELECT * FROM unrelated_history")
    records = []
    with writes["app"].transaction("tenant-a") as conn:
        for datasets in ([writes["dataset"]["id"]], ["unrelated"]):
            identity = uid()
            records.append(identity)
            conn.execute(
                schema.query_history.insert().values(
                    id=identity,
                    tenant="tenant-a",
                    actor="r",
                    input={"secret": "retained"},
                    dataset_ids=datasets,
                    status="complete",
                    logical_sql="SELECT",
                    created_at=now(),
                    updated_at=now(),
                )
            )
        QueryHistory.redact_dataset(conn, "tenant-b", writes["dataset"]["id"])
    preview = sql.execute("tenant-a", "DELETE FROM events WHERE id=72", actor="r")
    sql.commit("tenant-a", preview["preview_token"], "r")
    with writes["app"].transaction("tenant-a") as conn:
        histories = {
            row["id"]: row for row in conn.execute(schema.query_history.select()).mappings()
        }
        assert histories[records[0]]["input"] == {} and histories[records[1]]["input"]
        runs = {row["id"]: row for row in conn.execute(schema.runs.select()).mappings()}
        assert runs[old["run_id"]]["result"] == []
        assert runs[unaffected["run_id"]]["result"]


def test_concurrent_same_token_commits_once(writes):
    sql = writes["sql"]
    preview = sql.execute("tenant-a", "UPDATE events SET amount=amount+1 WHERE id=81", actor="r")

    def commit():
        try:
            sql.commit("tenant-a", preview["preview_token"], "r")
            return True
        except (ValueError, DBAPIError):
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(lambda _: commit(), range(2))) == 1
    assert read(writes, 81)["amount"] == Decimal("1.81")


def test_application_api_review_and_lock_conflict(writes):
    from fastapi.testclient import TestClient
    from sdd.api import create_app
    from sdd.execution import Executor

    headers = {"Authorization": "Bearer reviewer"}
    with TestClient(
        create_app(
            Executor(writes["app"], {}),
            tokens={
                "reviewer": {"tenant": "tenant-a", "name": "r", "role": "reviewer"},
                "reader": {"tenant": "tenant-a", "name": "reader", "role": "reader"},
            },
        )
    ) as client:
        body = {"sql": "UPDATE events SET amount=amount+1 WHERE id=83", "max_affected": 1}
        assert (
            client.post(
                "/data/sql", json=body, headers={"Authorization": "Bearer reader"}
            ).status_code
            == 403
        )
        preview = client.post("/data/sql", json=body, headers=headers)
        assert preview.status_code == 200, preview.text
        token = preview.json()["preview_token"]
        with writes["owner"].engine.connect() as blocker:
            blocker.exec_driver_sql(f"SELECT * FROM {writes['relation']} WHERE id=83 FOR UPDATE")
            response = client.post(f"/data/mutations/{token}/commit", headers=headers)
            assert response.status_code == 409 and response.json()["code"] == "55P03"
            blocker.rollback()
        fresh = client.post("/data/sql", json=body, headers=headers).json()
        result = client.post(f"/data/mutations/{fresh['preview_token']}/commit", headers=headers)
        assert result.status_code == 200, result.text
        assert result.json()["manifest"]["affected_rows"] == 1
        assert read(writes, 83)["amount"] == Decimal("1.83")


def test_simultaneous_commit_returns_one_success_and_one_conflict(writes, monkeypatch):
    from fastapi.testclient import TestClient
    from sdd.api import create_app
    from sdd.execution import Executor

    app = create_app(
        Executor(writes["app"], {}),
        tokens={"review": {"tenant": "tenant-a", "name": "reviewer", "role": "reviewer"}},
    )
    headers = {"Authorization": "Bearer review"}
    before = read(writes, 987654)["amount"]
    with TestClient(app) as client:
        preview = client.post(
            "/data/sql",
            headers=headers,
            json={"sql": "UPDATE events SET amount=amount+1 WHERE id=987654"},
        )
        assert preview.status_code == 200, preview.text
        token = preview.json()["preview_token"]
        barrier, pending = Barrier(2), mutation._pending

        def synchronized(*args):
            barrier.wait(timeout=10)
            return pending(*args)

        monkeypatch.setattr(mutation, "_pending", synchronized)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _: client.post(f"/data/mutations/{token}/commit", headers=headers),
                    range(2),
                )
            )
    assert sorted(result.status_code for result in results) == [200, 409]
    rejected = next(result for result in results if result.status_code == 409)
    assert rejected.json()["code"] == "40001"
    assert read(writes, 987654)["amount"] == before + 1
