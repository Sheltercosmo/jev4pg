"""Development cases for bounded result transfer on an isolated PostgreSQL server."""

import os
import tracemalloc

import psycopg
import pytest
from sqlalchemy import column, select, text

from test_deployment_postgres import installation as installation
from sdd.generic.browse import TableBrowser
from sdd.generic.catalog import Catalog
from sdd.generic.results import (
    RESULT_BYTES,
    OversizedResultRow,
    encoded_size,
    read_result,
    result_cursor,
    result_row,
)
from sdd.generic.sql import SQLService
from sdd.generic.query_jobs import QueryJobs
from sdd.query_control import QueryControl, QueryInterrupted
from sdd.query_worker import QueryWorker


pytestmark = pytest.mark.skipif(
    not os.getenv("SDD_TEST_ADMIN_URL"), reason="Dedicated PostgreSQL server required"
)


@pytest.mark.parametrize("alias", ["observation", "观测值"])
def test_types_nulls_parameters_and_marker_collision(installation, alias):
    sql = f'''SELECT CAST(:label AS text) AS "{alias}",
        9223372036854775807::bigint AS identifier,
        123456789.0000000001::numeric AS amount,
        DATE '2026-01-02' AS day, ARRAY[1,NULL,3] AS positions,
        jsonb_build_object('消息','待确认','flag',true) AS detail,
        NULL::text AS absent, false AS flag, 7 AS _sdd_result_bytes'''
    with installation["owner"].engine.begin() as conn:
        result = read_result(conn, sql, {"label": "未完成 🧪"})
        conn.exec_driver_sql("CREATE TEMP TABLE after_result (value integer) ON COMMIT DROP")
        conn.exec_driver_sql("INSERT INTO after_result VALUES (1)")
        assert conn.exec_driver_sql("SELECT value FROM after_result").scalar_one() == 1
    assert result.rows == [
        {
            alias: "未完成 🧪",
            "identifier": 9223372036854775807,
            "amount": "123456789.0000000001",
            "day": "2026-01-02",
            "positions": [1, None, 3],
            "detail": {"消息": "待确认", "flag": True},
            "absent": None,
            "flag": False,
            "_sdd_result_bytes": 7,
        }
    ]
    assert result.size == encoded_size(result.rows)
    assert not result.manifest()["truncated"]


@pytest.mark.parametrize("kind", ["text", "json"])
def test_oversized_value_is_suppressed_before_driver_decoding(installation, kind):
    expression = "repeat('x', 64 * 1024 * 1024)"
    if kind == "json":
        expression = "jsonb_build_object('body', " + expression + ")"
    source = text("SELECT " + expression + " AS payload").columns(column("payload"))
    query = select(*source.subquery().c).limit(1)
    with installation["owner"].engine.begin() as conn:
        tracemalloc.start()
        try:
            with result_cursor(conn, query) as (cursor, names, marker):
                assert isinstance(cursor.cursor, psycopg.ServerCursor)
                row = cursor.mappings().one()
                assert names == ["payload"] and row["payload"] is None
                assert row[marker] > 64 * 1024 * 1024
                with pytest.raises(OversizedResultRow):
                    result_row(row, marker)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert peak < 4 * 1024 * 1024


def test_first_oversized_row_errors_later_oversized_row_preserves_prefix(installation):
    with installation["owner"].engine.begin() as conn:
        with pytest.raises(OversizedResultRow):
            read_result(conn, "SELECT repeat('文', 2000000) AS body", {})
        result = read_result(
            conn,
            """SELECT n, CASE WHEN n=2 THEN repeat('文',2000000)
            ELSE 'small' END AS body FROM generate_series(1,3) n ORDER BY n""",
            {},
        )
    assert result.rows == [{"n": 1, "body": "small"}]
    assert result.limited_by == "bytes"


def test_byte_limit_retains_whole_rows_and_order(installation):
    with installation["owner"].engine.begin() as conn:
        result = read_result(
            conn,
            """SELECT n, repeat('界',500000) AS body
            FROM generate_series(1,8) n ORDER BY n DESC""",
            {},
        )
    assert [row["n"] for row in result.rows] == [8, 7]
    assert all(row["body"] == "界" * 500000 for row in result.rows)
    assert result.limited_by == "bytes"
    assert result.size == encoded_size(result.rows) <= RESULT_BYTES


def test_row_limit_does_not_change_window_population_or_explicit_limit(installation):
    sql = """SELECT n, count(*) OVER () AS population, sum(n) OVER () AS total
        FROM generate_series(1,1500) n ORDER BY n DESC"""
    with installation["owner"].engine.begin() as conn:
        result = read_result(conn, sql, {})
        explicit = read_result(conn, sql + " LIMIT 3 OFFSET 5", {})
        empty = read_result(conn, "SELECT NULL AS empty WHERE false", {})
    assert len(result.rows) == 1000 and result.limited_by == "rows"
    assert [row["n"] for row in result.rows] == list(range(1500, 500, -1))
    assert all(row["population"] == 1500 and row["total"] == 1125750 for row in result.rows)
    assert [row["n"] for row in explicit.rows] == [1495, 1494, 1493]
    assert explicit.limited_by is None
    assert empty.rows == [] and empty.columns == ["empty"] and empty.limited_by is None


@pytest.mark.parametrize("condition", ["true", "false"])
def test_duplicate_output_names_require_aliases(installation, condition):
    with installation["owner"].engine.begin() as conn, pytest.raises(ValueError, match="aliases"):
        read_result(conn, "SELECT 1 AS duplicate, 2 AS duplicate WHERE " + condition, {})


def test_result_fetch_checks_cancellation_and_closes_cursor(installation, monkeypatch):
    import sdd.generic.results as results

    control = QueryControl()
    original = results.result_row
    cursors = []

    def cancel_after_first(row, marker, **kwargs):
        value = original(row, marker, **kwargs)
        control.cancel()
        return value

    monkeypatch.setattr(results, "result_row", cancel_after_first)
    with installation["owner"].engine.begin() as conn:
        from sqlalchemy import event

        event.listen(conn, "after_cursor_execute", lambda c, cu, *args: cursors.append(cu))
        with pytest.raises(QueryInterrupted), control.activate():
            read_result(conn, "SELECT n FROM generate_series(1,20) n", {})
        assert any(isinstance(cursor, psycopg.ServerCursor) for cursor in cursors)
        assert all(cursor.closed for cursor in cursors)
        assert conn.exec_driver_sql("SELECT 1").scalar_one() == 1


def test_sql_service_and_browser_share_predecode_guard(installation):
    db = installation["app"]
    catalog = Catalog(db)
    dataset = catalog.create(
        "tenant-a", "payloads", [{"id": 1, "body": "seed"}], primary_key=["id"]
    )
    with db.transaction("tenant-a") as conn:
        table = catalog.table(dataset, conn)
        conn.execute(table.update().values(body=text("repeat('x',16*1024*1024)")))
    with pytest.raises(OversizedResultRow):
        SQLService(db).execute("tenant-a", "SELECT body FROM payloads")
    with pytest.raises(OversizedResultRow):
        TableBrowser(db).scan("tenant-a", dataset["id"])
    result = SQLService(db).execute("tenant-a", "SELECT id, LENGTH(body) AS bytes FROM payloads")
    assert result["result"] == [{"id": 1, "bytes": 16 * 1024 * 1024}]
    assert result["manifest"]["result_limited_by"] is None
    page = TableBrowser(db).scan("tenant-a", dataset["id"], columns=["id"])
    assert page["result"] == [{"id": 1}] and not page["has_more"]

    jobs = QueryJobs(db)
    too_large = jobs.submit(
        "tenant-a", "alice", "reader", {"sql": "SELECT body FROM payloads"}, "oversized"
    )
    near_limit = jobs.submit(
        "tenant-a",
        "alice",
        "reader",
        {"sql": "SELECT LEFT(body, 4194000) AS body FROM payloads"},
        "near-limit",
    )
    worker = QueryWorker(db)
    try:
        assert worker.work_one("tenant-a") and worker.work_one("tenant-a")
    finally:
        worker.close()
    failed = jobs.get("tenant-a", "alice", too_large["id"])
    assert failed["output_state"] == "NOT_EVALUATED" and failed["error"] == "RESULT_TOO_LARGE"
    success = jobs.get("tenant-a", "alice", near_limit["id"])
    assert success["job_state"] == "SUCCEEDED" and success["output_state"] == "VALUE"
    assert len(success["result"]["result"][0]["body"]) == 4194000
    assert encoded_size(success["result"]) > RESULT_BYTES


def test_json_escape_expansion_respects_serialized_budget(installation):
    with installation["owner"].engine.begin() as conn:
        with pytest.raises(OversizedResultRow):
            read_result(conn, "SELECT repeat(chr(1),1000000) AS body", {})
        result = read_result(
            conn,
            """SELECT n, repeat(chr(1),400000) AS body
            FROM generate_series(1,3) n ORDER BY n""",
            {},
        )
    assert len(result.rows) == 1 and result.rows[0]["n"] == 1
    assert result.size == encoded_size(result.rows) <= RESULT_BYTES
    assert result.limited_by == "bytes"
