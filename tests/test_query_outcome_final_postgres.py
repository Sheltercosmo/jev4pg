"""Fresh recovery cases after the outcome-delivery runtime freeze."""

import os

import pytest

from test_deployment_postgres import installation as installation
from test_query_outcome_postgres import population as population, publish, client, SQL, HEADERS
from sdd.generic.history import QueryHistory
from sdd.generic.query_jobs import QueryJobs
from sdd.generic.sql import SQLService
from sdd.query_worker import QueryWorker


pytestmark = pytest.mark.skipif(
    not os.getenv("SDD_TEST_ADMIN_URL"), reason="Dedicated PostgreSQL server required"
)


def test_cancellation_wins_over_later_held_outcome_publication(population):
    db, jobs = population["app"], QueryJobs(population["app"])
    saved = jobs.submit("tenant-a", "分析员", "reader", {"sql": SQL}, "held-cancellation")
    claim = jobs.claim("tenant-a")
    jobs.cancel("tenant-a", "分析员", saved["id"])
    with db.transaction("tenant-a") as conn:
        held = SQLService(db).save(
            conn,
            "tenant-a",
            "contract fixture",
            SQL,
            SQL,
            {},
            {},
            {
                "complete": False,
                "result_output_state": "NOT_EVALUATED",
                "result_operation_state": "BLOCKED_BY_DEPENDENCY",
                "result_hold_reason": "Old dependency reason",
            },
            [],
        )
    assert jobs.finish(claim, held)
    outcome = jobs.get("tenant-a", "分析员", saved["id"])
    assert outcome["job_state"] == "CANCELLED" and outcome["output_state"] == "NOT_EVALUATED"
    assert outcome["operation_state"] == "CANCELLED" and outcome["hold_reason"] is None
    assert outcome["result"] is None
    history = QueryHistory(db).detail("tenant-a", "分析员", saved["id"])
    assert history["status"] == "cancelled" and "result" not in history["output"]


def test_corrected_query_returns_real_zero_without_mutating_original_hold(population):
    identity = publish(
        population,
        "budget-held-parent",
        {
            "complete": False,
            "result_output_state": "NOT_EVALUATED",
            "result_operation_state": "BLOCKED_BY_BUDGET",
            "result_hold_reason": "需要增加判断额度或修改查询",
        },
    )
    api = client(population)
    original = api.get(f"/query-history/{identity}", headers=HEADERS).json()
    assert original["status"] == "held"
    revised_sql = 'SELECT COUNT(*) AS total FROM "待办" WHERE "编号" < 0'
    submitted = api.post(
        "/data/query-jobs",
        headers=HEADERS,
        json={
            "sql": revised_sql,
            "idempotency_key": "exact-revision",
            "parent_history_id": identity,
        },
    )
    assert submitted.status_code == 202
    revised_id = submitted.json()["id"]
    worker = QueryWorker(population["app"])
    try:
        assert worker.work_one("tenant-a")
    finally:
        worker.close()
    actual = api.get(f"/data/query-jobs/{revised_id}", headers=HEADERS).json()
    assert actual["output_state"] == "VALUE" and actual["operation_state"] == "SUCCEEDED"
    assert actual["hold_reason"] is None and actual["result"]["result"] == [{"total": 0}]
    revised = api.get(f"/query-history/{revised_id}", headers=HEADERS).json()
    assert revised["parent_id"] == identity and revised["status"] == "complete"
    assert api.get(f"/query-history/{identity}", headers=HEADERS).json() == original
    parent = api.get(f"/data/query-jobs/{identity}", headers=HEADERS).json()
    assert (
        parent["output_state"] == "NOT_EVALUATED"
        and parent["operation_state"] == "BLOCKED_BY_BUDGET"
    )
