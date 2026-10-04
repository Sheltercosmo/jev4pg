"""Presentation contracts for saved executor outcomes; no model or native inference."""

import pytest
from sqlalchemy import update

from test_query_jobs import source as source, submit, QUERY
from sdd.generic import schema
from sdd.generic.history import QueryHistory
from sdd.generic.query_jobs import QueryJobs
from sdd.generic.query_outcome import history_status, query_states
from sdd.generic.sql import SQLService


def saved_outcome(db, manifest, rows):
    with db.transaction("team") as conn:
        return SQLService(db).save(
            conn, "team", "fixture", QUERY["sql"], "SELECT 0", {}, {}, manifest, rows
        )


@pytest.mark.parametrize(
    "output,operation,complete,truncated,status,rows",
    [
        ("NOT_EVALUATED", "BLOCKED_BY_DEPENDENCY", False, True, "held", []),
        ("NOT_EVALUATED", "BLOCKED_BY_BUDGET", False, False, "held", []),
        ("NOT_EVALUATED", "FAILED", False, False, "error", []),
        ("UNKNOWN", "PARTIAL", False, False, "partial", [{"value": None}]),
        ("VALUE", "SUCCEEDED", True, False, "complete", [{"value": False, "total": 0}]),
        ("VALUE", "SUCCEEDED", True, True, "complete", [{"value": True}]),
    ],
)
def test_job_detail_list_and_history_preserve_executor_states(
    source, output, operation, complete, truncated, status, rows
):
    db, _ = source
    saved = submit(db)
    jobs = QueryJobs(db)
    claim = jobs.claim("team")
    reason = "必须先完成分组判断" if output == "NOT_EVALUATED" else None
    manifest = {
        "result_output_state": output,
        "result_operation_state": operation,
        "result_hold_reason": reason,
        "complete": complete,
        "truncated": truncated,
    }
    result = saved_outcome(db, manifest, rows)
    assert jobs.finish(claim, result)
    detail = jobs.get("team", "alice", saved["id"])
    summary = jobs.recent("team", "alice")["items"][0]
    expected_operation = "TRUNCATED" if complete and truncated else operation
    for record in (detail, summary):
        assert record["job_state"] == "SUCCEEDED"
        assert record["output_state"] == output
        assert record["operation_state"] == expected_operation
        assert record["hold_reason"] == reason
    assert summary["result"] is None and detail["result"]["result"] == rows
    history = QueryHistory(db)
    reopened = history.detail("team", "alice", saved["id"])
    assert reopened["status"] == history.recent("team", "alice")["items"][0]["status"] == status
    assert reopened["output"]["output_state"] == output
    assert reopened["output"]["operation_state"] == expected_operation
    assert reopened["output"]["result"] == rows and reopened["output"]["executed"] is False


def test_existing_partial_history_is_presented_as_held_without_reexecuting(source):
    db, _ = source
    manifest = {
        "complete": False,
        "result_output_state": "NOT_EVALUATED",
        "result_operation_state": "BLOCKED_BY_DEPENDENCY",
        "result_hold_reason": "Waiting for membership",
    }
    history = QueryHistory(db)
    result = history.capture(
        "team",
        "alice",
        {"mode": "sql", "text": QUERY["sql"]},
        lambda: saved_outcome(db, manifest, []),
    )
    identity = result["history_id"]
    with db.transaction("team") as conn:
        conn.execute(
            update(schema.query_history)
            .where(schema.query_history.c.id == identity)
            .values(status="partial")
        )
    assert history.get("team", "alice", identity)["status"] == "partial"
    assert history.recent("team", "alice")["items"][0]["status"] == "held"
    reopened = history.detail("team", "alice", identity)
    assert reopened["status"] == "held" and reopened["output"]["output_state"] == "NOT_EVALUATED"
    assert reopened["output"]["hold_reason"] == "Waiting for membership"
    assert history.get("team", "alice", identity)["status"] == "partial"
    revised = history.capture(
        "team",
        "alice",
        {"mode": "sql", "text": "SELECT COUNT(*) AS n FROM records"},
        lambda: SQLService(db).execute("team", "SELECT COUNT(*) AS n FROM records"),
        parent_id=identity,
    )
    assert revised["result"] == [{"n": 3}]
    assert history.detail("team", "alice", revised["history_id"])["parent_id"] == identity
    assert history.detail("team", "alice", identity) == reopened


def test_legacy_partial_empty_and_preview_contracts_remain_distinct():
    assert query_states({"manifest": {"complete": None}})["output_state"] == "NOT_EVALUATED"
    assert (
        query_states({"manifest": {"complete": False}, "result": []})["output_state"] == "UNKNOWN"
    )
    assert query_states({"manifest": {"complete": True}, "result": []})["output_state"] == "VALUE"
    assert (
        history_status({"manifest": {"result_output_state": "VALUE"}}, previous="preview")
        == "preview"
    )
    assert (
        history_status({"manifest": {"result_output_state": "VALUE"}}, previous="committed")
        == "committed"
    )
    assert (
        query_states({"mutation_preview": True, "manifest": {"complete": True}})["operation_state"]
        == "AWAITING_REVIEW"
    )


@pytest.mark.parametrize("state", ["UNRECOGNIZED", ["VALUE"]])
def test_invalid_saved_state_cannot_be_promoted_from_complete(state):
    actual = query_states({"manifest": {"complete": True, "result_output_state": state}})
    assert actual["output_state"] == "NOT_EVALUATED" and actual["operation_state"] == "FAILED"


def test_preview_or_truncation_does_not_hide_operational_failure():
    result = {
        "mutation_preview": True,
        "manifest": {
            "complete": True,
            "truncated": True,
            "result_output_state": "VALUE",
            "result_operation_state": "FAILED",
        },
    }
    assert query_states(result)["output_state"] == "VALUE"
    assert query_states(result)["operation_state"] == "FAILED"
    assert history_status(result) == "error"
