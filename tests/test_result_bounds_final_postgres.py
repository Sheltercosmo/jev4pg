"""Fresh relational and delivery cases first run after the result-reader freeze."""

import os
import tracemalloc

import pytest
from sqlalchemy import text

from test_deployment_postgres import installation as installation
from sdd.generic.browse import TableBrowser
from sdd.generic.catalog import Catalog, serial
from sdd.generic.history import QueryHistory
from sdd.generic.query_jobs import QueryJobs
from sdd.generic.results import RESULT_BYTES, OversizedResultRow, encoded_size, read_result
from sdd.generic.sql import SQLService
from sdd.query_worker import QueryWorker


pytestmark = pytest.mark.skipif(
    not os.getenv("SDD_TEST_ADMIN_URL"), reason="Dedicated PostgreSQL server required"
)


def test_join_duplicates_nulls_and_window_frames_match_direct_postgresql(installation):
    sql = """WITH readings AS (
        SELECT * FROM (VALUES (1,'a',1.01::numeric),(2,'a',NULL),
            (3,'b',3.03::numeric),(4,'b',3.03::numeric)) AS v(id,zone,amount)
    ), expanded AS (
        SELECT r.*, c.copy FROM readings r CROSS JOIN (VALUES(1),(2)) c(copy)
    ) SELECT id,copy,zone,amount,
        SUM(amount) OVER (PARTITION BY zone ORDER BY id,copy ROWS UNBOUNDED PRECEDING) AS running,
        DENSE_RANK() OVER (ORDER BY amount NULLS LAST) AS position
        FROM expanded ORDER BY zone,id DESC,copy"""
    with installation["owner"].engine.begin() as conn:
        expected = serial([dict(row) for row in conn.execute(text(sql)).mappings()])
        guarded = read_result(conn, sql, {})
    assert guarded.rows == expected and len(guarded.rows) == 8
    assert sum(row["amount"] is None for row in guarded.rows) == 2
    assert guarded.limited_by is None


def test_small_first_row_does_not_allow_later_wide_rows_to_exceed_transfer_budget(installation):
    sql = """SELECT n, CASE WHEN n=1 THEN 'tiny'
        ELSE repeat(chr(64 + (n % 26)),800000) END AS body
        FROM generate_series(1,250) n"""
    with installation["owner"].engine.begin() as conn:
        tracemalloc.start()
        try:
            result = read_result(conn, sql, {})
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert [row["n"] for row in result.rows] == [1, 2, 3, 4, 5, 6]
    assert result.rows[0]["body"] == "tiny"
    assert all(row["body"] == chr(64 + row["n"] % 26) * 800000 for row in result.rows[1:])
    assert encoded_size(result.rows) == result.size <= RESULT_BYTES
    assert peak < 16 * 1024 * 1024 and result.limited_by == "bytes"


def test_large_aggregate_is_guarded_and_reserved_aliases_remain_data(installation):
    with installation["owner"].engine.begin() as conn:
        with pytest.raises(OversizedResultRow):
            read_result(
                conn,
                """SELECT string_agg(repeat(n::text,250000),'') AS message
                FROM generate_series(1,40) n""",
                {},
            )
        result = read_result(
            conn,
            """SELECT '原文' AS _sdd_result_bytes,
            NULL::integer AS _sdd_result_bytes_,
            jsonb_build_array(9223372036854775807::bigint,NULL,false) AS _sdd_size""",
            {},
        )
    assert result.rows == [
        {
            "_sdd_result_bytes": "原文",
            "_sdd_result_bytes_": None,
            "_sdd_size": [9223372036854775807, None, False],
        }
    ]
    assert result.size == encoded_size(result.rows)


@pytest.mark.parametrize("names", [("Incoming", "id", "body"), ("来件", "编号", "正文")])
def test_page_stops_before_oversized_record_and_resumes_without_skipping(installation, names):
    logical, key, body = names
    db, catalog = installation["app"], Catalog(installation["app"])
    dataset = catalog.create(
        "tenant-a", logical, [{key: n, body: "正常"} for n in (1, 2, 3)], primary_key=[key]
    )
    with db.transaction("tenant-a") as conn:
        table = catalog.table(dataset, conn)
        conn.execute(
            table.update().where(table.c[key] == 2).values({body: text("repeat('大',2000000)")})
        )
    browser = TableBrowser(db)
    page = browser.scan("tenant-a", dataset["id"])
    assert page["result"] == [{key: 1, body: "正常"}]
    assert page["next_after"] == [1] and page["page_limited_by"] == "bytes"
    with pytest.raises(OversizedResultRow):
        browser.scan("tenant-a", dataset["id"], after=page["next_after"])
    reduced = browser.scan("tenant-a", dataset["id"], columns=[key], after=page["next_after"])
    assert reduced["result"] == [{key: 2}, {key: 3}] and not reduced["has_more"]
    summary = SQLService(db).execute("tenant-a", f'SELECT COUNT(*) AS total FROM "{logical}"')
    assert summary["result"] == [{"total": 3}]


def test_background_byte_truncation_preserves_population_and_history(installation):
    db, catalog = installation["app"], Catalog(installation["app"])
    dataset = catalog.create(
        "tenant-a",
        "配送记录",
        [{"单号": n, "备注": "seed"} for n in range(1, 6)],
        primary_key=["单号"],
    )
    with db.transaction("tenant-a") as conn:
        table = catalog.table(dataset, conn)
        conn.execute(table.update().values({"备注": text("repeat('待',700000)")}))
    jobs = QueryJobs(db)
    saved = jobs.submit(
        "tenant-a",
        "复核员",
        "reader",
        {
            "sql": 'SELECT "单号", "备注", COUNT(*) OVER () AS total FROM "配送记录" ORDER BY "单号" DESC'
        },
        "完整范围",
    )
    worker = QueryWorker(db)
    try:
        assert worker.work_one("tenant-a")
    finally:
        worker.close()
    outcome = jobs.get("tenant-a", "复核员", saved["id"])
    assert outcome["job_state"] == "SUCCEEDED" and outcome["output_state"] == "VALUE"
    assert outcome["operation_state"] == "TRUNCATED"
    result = outcome["result"]
    assert result["result"] == [{"单号": 5, "备注": "待" * 700000, "total": 5}]
    assert result["manifest"]["result_limited_by"] == "bytes"
    history = QueryHistory(db).detail("tenant-a", "复核员", saved["id"])
    assert history["output"]["result"] == result["result"]
    assert (
        jobs.submit(
            "tenant-a",
            "复核员",
            "reader",
            {"sql": result["logical_sql"]},
            "完整范围",
        )["id"]
        == saved["id"]
    )
