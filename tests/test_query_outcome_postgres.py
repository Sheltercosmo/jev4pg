"""Real PostgreSQL/API delivery of executor-contract fixtures, without native inference."""

import os
import tracemalloc

import pytest
from fastapi.testclient import TestClient

from test_deployment_postgres import installation as installation
from sdd.api import create_app
from sdd.execution import Executor
from sdd.generic.catalog import Catalog
from sdd.generic.history import QueryHistory
from sdd.generic.query_jobs import QueryJobs
from sdd.generic.sql import SQLService


pytestmark = pytest.mark.skipif(
    not os.getenv("SDD_TEST_ADMIN_URL"), reason="Dedicated PostgreSQL server required"
)


@pytest.fixture(scope="module")
def population(installation):
    Catalog(installation["app"]).create(
        "tenant-a", "待办", [{"编号": 1, "内容": "待确认"}], primary_key=["编号"]
    )
    return installation


def client(env):
    return TestClient(
        create_app(
            Executor(env["app"], {}),
            tokens={
                "owner": {"tenant": "tenant-a", "name": "分析员", "role": "reader"},
                "peer": {"tenant": "tenant-a", "name": "同事", "role": "reader"},
                "other": {"tenant": "tenant-b", "name": "分析员", "role": "reader"},
            },
        )
    )


SQL = 'SELECT "编号" FROM "待办"'
HEADERS = {"Authorization": "Bearer owner"}


def publish(env, key, manifest, rows=None, plan=None):
    db, jobs = env["app"], QueryJobs(env["app"])
    job = jobs.submit("tenant-a", "分析员", "reader", {"sql": SQL}, key)
    claim = jobs.claim("tenant-a")
    assert claim["id"] == job["id"]
    with db.transaction("tenant-a") as conn:
        result = SQLService(db).save(
            conn, "tenant-a", "contract fixture", SQL, SQL, {}, plan or {}, manifest, rows or []
        )
    assert jobs.finish(claim, result)
    return job["id"]


@pytest.mark.parametrize(
    "output,operation,complete,history",
    [
        ("NOT_EVALUATED", "BLOCKED_BY_DEPENDENCY", False, "held"),
        ("NOT_EVALUATED", "FAILED", False, "error"),
        ("UNKNOWN", "PARTIAL", False, "partial"),
        ("VALUE", "SUCCEEDED", True, "complete"),
    ],
)
def test_api_restart_list_and_history_retain_explicit_result_states(
    population, output, operation, complete, history
):
    reason = "后续计算依赖尚未完成的判断" if output == "NOT_EVALUATED" else None
    identity = publish(
        population,
        operation,
        {
            "complete": complete,
            "result_output_state": output,
            "result_operation_state": operation,
            "result_hold_reason": reason,
        },
    )
    original = client(population).get(f"/data/query-jobs/{identity}", headers=HEADERS)
    assert original.status_code == 200
    reopened = client(population)
    detail = reopened.get(f"/data/query-jobs/{identity}", headers=HEADERS).json()
    assert detail == original.json()
    page = reopened.get("/data/query-jobs", headers=HEADERS).json()["items"]
    summary = next(row for row in page if row["id"] == identity)
    for record in (detail, summary):
        assert record["output_state"] == output and record["operation_state"] == operation
        assert record["hold_reason"] == reason and record["job_state"] == "SUCCEEDED"
    saved = reopened.get(f"/query-history/{identity}", headers=HEADERS).json()
    assert saved["status"] == history and saved["output"]["output_state"] == output
    assert saved["output"]["result"] == [] and saved["output"]["executed"] is False
    histories = reopened.get("/query-history", headers=HEADERS).json()["items"]
    assert next(row for row in histories if row["id"] == identity)["status"] == history
    for token in ("peer", "other"):
        headers = {"Authorization": "Bearer " + token}
        assert reopened.get(f"/data/query-jobs/{identity}", headers=headers).status_code == 400
        assert reopened.get(f"/query-history/{identity}", headers=headers).status_code == 400
    assert (
        reopened.post(
            "/data/query-jobs", headers=HEADERS, json={"sql": SQL, "idempotency_key": operation}
        ).json()["id"]
        == identity
    )


def test_status_lists_bound_reasons_and_do_not_load_large_saved_plans(population):
    reason = "需要复核" * 100000
    identity = publish(
        population,
        "large-evidence",
        {
            "complete": False,
            "result_output_state": "NOT_EVALUATED",
            "result_operation_state": "BLOCKED_BY_BUDGET",
            "result_hold_reason": reason,
        },
        plan={"evidence_fixture": "x" * 1500000},
    )
    jobs, history = QueryJobs(population["app"]), QueryHistory(population["app"])
    jobs.recent("tenant-a", "分析员")
    history.recent("tenant-a", "分析员")
    tracemalloc.start()
    try:
        listed = jobs.recent("tenant-a", "分析员")["items"]
        historical = history.recent("tenant-a", "分析员")["items"]
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    row = next(row for row in listed if row["id"] == identity)
    assert row["hold_reason"] == reason[:1000] and row["result"] is None
    assert row["output_state"] == "NOT_EVALUATED"
    assert next(row for row in historical if row["id"] == identity)["status"] == "held"
    assert peak < 1000000


def test_malformed_operation_has_same_failed_state_in_details_and_lists(population):
    identity = publish(
        population,
        "malformed-operation",
        {"complete": True, "result_output_state": "VALUE", "result_operation_state": ["FAILED"]},
    )
    jobs = QueryJobs(population["app"])
    detail = jobs.get("tenant-a", "分析员", identity)
    listed = next(
        row for row in jobs.recent("tenant-a", "分析员")["items"] if row["id"] == identity
    )
    for record in (detail, listed):
        assert record["output_state"] == "NOT_EVALUATED" and record["operation_state"] == "FAILED"
