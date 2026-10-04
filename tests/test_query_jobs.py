from sqlalchemy import select, update
import pytest

from sdd.db import Database
from sdd.generic import schema
from sdd.generic.catalog import Catalog
from sdd.generic.history import QueryHistory
from sdd.generic.query_jobs import QueryJobs, QueryJobConflict
from sdd.generic.sql import SQLService
from sdd.query_worker import QueryWorker


@pytest.fixture
def source(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "jobs.db"))
    db.initialize()
    dataset = Catalog(db).create(
        "team",
        "records",
        [{"id": 1, "value": 12}, {"id": 2, "value": None}, {"id": 3, "value": 12}],
        primary_key=["id"],
    )
    yield db, dataset
    db.engine.dispose()


QUERY = {"sql": "SELECT value, COUNT(*) AS n FROM records GROUP BY value ORDER BY value"}


def submit(db, key="first", request=None, actor="alice", role="reader"):
    return QueryJobs(db).submit("team", actor, role, request or QUERY, key)


def test_idempotency_durable_results_and_history(source):
    db, _ = source
    first = submit(db)
    assert first["job_state"] == "QUEUED" and first["result"] is None
    assert first["output_state"] == "NOT_EVALUATED"
    assert submit(db)["id"] == first["id"]
    worker = QueryWorker(db)
    assert worker.work_one("team") and not worker.work_one("team")
    restarted = Database(db.engine.url)
    try:
        result = QueryJobs(restarted).get("team", "alice", first["id"])
        assert result["job_state"] == "SUCCEEDED" and result["output_state"] == "VALUE"
        assert result["result"]["result"] == [{"value": 12, "n": 2}, {"value": None, "n": 1}]
        assert (
            QueryHistory(restarted).get("team", "alice", first["history_id"])["status"]
            == "complete"
        )
        assert submit(restarted)["id"] == first["id"]
    finally:
        restarted.engine.dispose()


def test_actor_scope_request_conflict_and_preview_permission(source):
    db, _ = source
    first = submit(db)
    assert submit(db, actor="bob")["id"] != first["id"]
    with pytest.raises(QueryJobConflict):
        submit(db, request={"sql": "SELECT id FROM records"})
    for tenant, actor in (("other", "alice"), ("team", "bob")):
        with pytest.raises(ValueError, match="unavailable"):
            QueryJobs(db).get(tenant, actor, first["id"])
        with pytest.raises(ValueError, match="unavailable"):
            QueryJobs(db).cancel(tenant, actor, first["id"])
    with pytest.raises(PermissionError):
        submit(db, "write", {"sql": "UPDATE records SET value=4 WHERE id=1"})


def test_cancel_queued_and_replay_does_not_resubmit(source):
    db, _ = source
    job = submit(db)
    result = QueryJobs(db).cancel("team", "alice", job["id"])
    assert result["job_state"] == "CANCELLED" and result["output_state"] == "NOT_EVALUATED"
    assert submit(db)["job_state"] == "CANCELLED"
    assert not QueryWorker(db).work_one("team")


def test_expired_lease_rejects_late_results_and_never_requeues(source):
    db, _ = source
    saved = submit(db)
    jobs = QueryJobs(db)
    claim = jobs.claim("team")
    result = SQLService(db).execute("team", QUERY["sql"])
    with db.transaction("team") as conn:
        conn.execute(update(schema.query_jobs).values(lease_until=0))
    assert not jobs.finish(claim, result)
    assert jobs.heartbeat(claim, 30) is None
    assert jobs.claim("team") is None
    final = jobs.get("team", "alice", saved["id"])
    assert final["job_state"] == "FAILED" and final["error"] == "LEASE_EXPIRED"
    assert final["result"] is None and final["output_state"] == "NOT_EVALUATED"


def test_cancellation_wins_result_publication_and_wrong_owner_cannot_finish(source):
    db, _ = source
    saved = submit(db)
    jobs = QueryJobs(db)
    claim = jobs.claim("team")
    result = SQLService(db).execute("team", QUERY["sql"])
    assert not jobs.finish({**claim, "lease_token": "someone-else"}, result)
    assert jobs.cancel("team", "alice", saved["id"])["job_state"] == "CANCELLING"
    assert jobs.finish(claim, result)
    final = jobs.get("team", "alice", saved["id"])
    assert final["job_state"] == "CANCELLED" and final["result"] is None


def test_preview_requires_commit_and_deletion_redacts_published_job(source):
    db, _ = source
    saved = submit(db, "preview", {"sql": "DELETE FROM records WHERE id=1"}, role="reviewer")
    QueryWorker(db).work_one("team")
    jobs = QueryJobs(db)
    preview = jobs.get("team", "alice", saved["id"])
    assert preview["operation_state"] == "AWAITING_REVIEW"
    service = SQLService(db)
    assert len(service.execute("team", "SELECT * FROM records")["result"]) == 3
    service.commit("team", preview["result"]["preview_token"], "alice")
    final = jobs.get("team", "alice", saved["id"])
    assert final["job_state"] == "REDACTED" and final["result"] is None
    with db.transaction("team") as conn:
        record = conn.execute(select(schema.query_jobs)).mappings().one()
        assert record["request"] == {} and record["outcome"] is None


def test_changed_catalog_does_not_rebind_queued_sql(source):
    db, dataset = source
    saved = submit(db)
    with db.transaction("team") as conn:
        conn.execute(
            update(schema.datasets)
            .where(schema.datasets.c.id == dataset["id"])
            .values(description="Different business meaning")
        )
    QueryWorker(db).work_one("team")
    outcome = QueryJobs(db).get("team", "alice", saved["id"])
    assert outcome["job_state"] == "FAILED" and outcome["result"] is None
    assert outcome["error"] == "ValueError"


def test_unexecuted_semantic_work_is_unknown_not_a_complete_empty_result(source):
    db, _ = source

    class Model:
        model = "fixture-no-call"

        def ask(self, *args, **kwargs):
            raise AssertionError("Zero admission budget must not call the provider")

    saved = submit(
        db,
        "held",
        {"sql": "SELECT id FROM records WHERE SEMANTIC(value, 'Relevant')", "max_evaluations": 0},
    )
    QueryWorker(db, Model()).work_one("team")
    result = QueryJobs(db).get("team", "alice", saved["id"])
    assert result["job_state"] == "SUCCEEDED"
    assert result["output_state"] == "UNKNOWN" and result["operation_state"] == "PARTIAL"
    assert result["result"]["manifest"]["result_is_partial"]


def test_recent_pages_preserve_scope_and_completion_states(source):
    db, _ = source
    identities = {submit(db, f"page-{n}")["id"] for n in range(3)}
    submit(db, "other-actor", actor="bob")
    QueryWorker(db).work_one("team")
    jobs, seen, cursor = QueryJobs(db), set(), None
    while True:
        page = jobs.recent("team", "alice", 1, cursor)
        assert len(page["items"]) == 1 and page["items"][0]["result"] is None
        seen.add(page["items"][0]["id"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == identities
