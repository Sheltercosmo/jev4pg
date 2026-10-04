from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import secrets
import subprocess
import sys
from threading import Barrier, Event, Lock
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update

from test_deployment_postgres import installation as installation
from test_migration_preflight_postgres import target as target
from sdd.api import create_app
from sdd.bootstrap import SCHEMA_VERSION, check_migration, migrate
from sdd.db import Database
from sdd.execution import Executor
from sdd.generic import schema
from sdd.generic.catalog import Catalog
from sdd.generic.query_jobs import QueryJobs
from sdd.query_worker import QueryWorker


pytestmark = pytest.mark.skipif(
    not os.getenv("SDD_TEST_ADMIN_URL"), reason="Dedicated PostgreSQL server required"
)


@pytest.fixture(scope="module")
def population(installation):
    db = installation["app"]
    dataset = Catalog(db).create(
        "tenant-a", "events", [], primary_key=["id"], columns=[{"name": "id", "type": "integer"}]
    )
    with installation["owner"].engine.begin() as conn:
        conn.exec_driver_sql(
            f'INSERT INTO sdd_data."{dataset["table_name"]}" '
            "SELECT n FROM generate_series(1,100000) n"
        )
    return installation


def submit(env, key, sql="SELECT COUNT(*) AS n FROM events", **options):
    return QueryJobs(env["app"]).submit("tenant-a", "alice", "reader", {"sql": sql, **options}, key)


def test_concurrent_retry_race_uses_persisted_identity(population, monkeypatch):
    jobs = QueryJobs(population["app"])
    barrier, prepare = Barrier(6), jobs.sql.prepare

    def prepare_together(*args):
        value = prepare(*args)
        barrier.wait(timeout=10)
        return value

    monkeypatch.setattr(jobs.sql, "prepare", prepare_together)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(
            pool.map(
                lambda _: jobs.submit(
                    "tenant-a",
                    "racing-actor",
                    "reader",
                    {"sql": "SELECT id FROM events WHERE id=7"},
                    "simultaneous-retry",
                ),
                range(6),
            )
        )
    assert len({item["id"] for item in results}) == 1
    with population["app"].transaction("tenant-a") as conn:
        histories = (
            conn.execute(
                select(schema.query_history.c.id).where(
                    schema.query_history.c.actor == "racing-actor"
                )
            )
            .scalars()
            .all()
        )
    assert histories == [results[0]["id"]]
    jobs.cancel("tenant-a", "racing-actor", results[0]["id"])


def test_parallel_retries_and_exclusive_claims(population):
    with ThreadPoolExecutor(max_workers=6) as pool:
        submitted = list(pool.map(lambda _: submit(population, "same-request"), range(12)))
    assert len({job["id"] for job in submitted}) == 1
    for n in range(6):
        submit(population, f"claim-{n}")
    jobs = QueryJobs(population["app"])
    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(lambda _: jobs.claim("tenant-a"), range(8)))
    claims = [job for job in claimed if job]
    assert len(claims) == len({job["id"] for job in claims}) == 7
    for job in claims:
        assert jobs.finish(job, state="FAILED", error="TEST_DISCARD")


def test_http_restart_history_and_tenant_actor_boundaries(population):
    tokens = {
        "alice": {"tenant": "tenant-a", "name": "alice", "role": "reader"},
        "bob": {"tenant": "tenant-a", "name": "bob", "role": "reader"},
        "outsider": {"tenant": "tenant-b", "name": "alice", "role": "reader"},
    }
    headers = {"Authorization": "Bearer alice"}
    client = TestClient(create_app(Executor(population["app"], {}), tokens=tokens))
    body = {"sql": "SELECT id FROM events WHERE id=42", "idempotency_key": "http"}
    response = client.post("/data/query-jobs", json=body, headers=headers)
    assert response.status_code == 202, response.text
    identity = response.json()["id"]
    conflict = client.post(
        "/data/query-jobs", json={**body, "sql": "SELECT id FROM events"}, headers=headers
    )
    assert conflict.status_code == 409
    for token in ("bob", "outsider"):
        denied = {"Authorization": "Bearer " + token}
        assert client.get(f"/data/query-jobs/{identity}", headers=denied).status_code == 400
        assert client.post(f"/data/query-jobs/{identity}/cancel", headers=denied).status_code == 400
    denied_preview = client.post(
        "/data/query-jobs",
        json={"sql": "DELETE FROM events WHERE id=1", "idempotency_key": "forbidden"},
        headers=headers,
    )
    assert denied_preview.status_code == 403
    worker = QueryWorker(population["app"])
    try:
        assert worker.work_one("tenant-a")
    finally:
        worker.close()
    replacement_db = Database(population["app"].engine.url)
    try:
        restarted = TestClient(create_app(Executor(replacement_db, {}), tokens=tokens))
        result = restarted.get(f"/data/query-jobs/{identity}", headers=headers).json()
        assert result["job_state"] == "SUCCEEDED" and result["result"]["result"] == [{"id": 42}]
        saved = restarted.get(f"/query-history/{identity}", headers=headers).json()
        assert saved["status"] == "complete" and saved["input"]["text"] == body["sql"]
        assert (
            restarted.post("/data/query-jobs", json=body, headers=headers).json()["id"] == identity
        )
    finally:
        replacement_db.engine.dispose()


def child_worker(env, *, script=None):
    script = (
        script
        or """
import os
from sdd.db import Database
from sdd.query_worker import QueryWorker
db = Database(os.environ['SDD_QUERY_JOB_TEST_URL'])
worker = QueryWorker(db, poll_seconds=0.05)
try:
    assert worker.work_one('tenant-a')
finally:
    worker.close()
    db.engine.dispose()
"""
    )
    return subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env={
            **os.environ,
            "SDD_QUERY_JOB_TEST_URL": env["app"].engine.url.render_as_string(hide_password=False),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def wait_query(env):
    until = monotonic() + 8
    while monotonic() < until:
        with env["owner"].engine.connect() as conn:
            active = conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE usename=:role "
                    "AND state='active' "
                    "AND (query LIKE 'FETCH %' OR query LIKE 'SELECT * FROM (%_sdd_result%')"
                ),
                {"role": env["roles"][0]},
            ).scalar_one()
        if active:
            return
        sleep(0.02)
    raise AssertionError("Expected child SQL execution to become active")


def test_cancel_running_query_from_another_process(population):
    job = submit(
        population, "cross-process", "SELECT COUNT(*) FROM events a JOIN events b ON a.id<>b.id"
    )
    process = child_worker(population)
    try:
        wait_query(population)
        requested = QueryJobs(population["app"]).cancel("tenant-a", "alice", job["id"])
        assert requested["job_state"] in ("CANCELLING", "CANCELLED")
        process.communicate(timeout=5)
        assert process.returncode == 0
        finished = QueryJobs(population["app"]).get("tenant-a", "alice", job["id"])
        assert finished["job_state"] == "CANCELLED" and finished["result"] is None
        assert finished["output_state"] == "NOT_EVALUATED"
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=5)


def test_worker_death_never_reexecutes_an_expired_claim(population):
    job = submit(population, "worker-death")
    process = child_worker(
        population,
        script="""
import os, time
from sdd.db import Database
from sdd.generic.query_jobs import QueryJobs
db = Database(os.environ['SDD_QUERY_JOB_TEST_URL'])
assert QueryJobs(db).claim('tenant-a', 5)
print('claimed', flush=True)
time.sleep(30)
""",
    )
    try:
        until = monotonic() + 5
        while monotonic() < until:
            if (
                QueryJobs(population["app"]).get("tenant-a", "alice", job["id"])["job_state"]
                == "RUNNING"
            ):
                break
            sleep(0.02)
        else:
            raise AssertionError("Expected child to own a durable claim")
        process.terminate()
        process.communicate(timeout=5)
        with population["owner"].engine.begin() as conn:
            conn.execute(
                update(schema.query_jobs)
                .where(schema.query_jobs.c.id == job["id"])
                .values(lease_until=0)
            )
        replacement = QueryWorker(population["app"])
        try:
            assert not replacement.work_one("tenant-a")
        finally:
            replacement.close()
        failed = QueryJobs(population["app"]).get("tenant-a", "alice", job["id"])
        assert failed["job_state"] == "FAILED" and failed["error"] == "LEASE_EXPIRED"
        assert submit(population, "worker-death")["id"] == job["id"]
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=5)


def test_version_three_upgrade_preserves_catalog_and_adds_restricted_jobs(target):
    url, engine, role, _ = target
    password = secrets.token_urlsafe(32)
    migrate(url, role, password)
    db = Database(url.set(username=role, password=password))
    try:
        dataset = Catalog(db).create("tenant-a", "original", [{"id": 1}], primary_key=["id"])
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE sdd_catalog.dataset_query_jobs")
            conn.exec_driver_sql("UPDATE sdd_catalog.sdd_schema_version SET version=3")
            original_oid = conn.exec_driver_sql(
                "SELECT 'sdd_catalog.dataset_catalog'::regclass::oid"
            ).scalar_one()
        assert check_migration(url, role)["schema_version"] == 3
        migrate(url, role)
        assert check_migration(url, role)["schema_version"] == SCHEMA_VERSION
        assert Catalog(db).get("tenant-a", dataset["id"])["name"] == "original"
        with engine.connect() as conn:
            assert (
                conn.exec_driver_sql(
                    "SELECT 'sdd_catalog.dataset_catalog'::regclass::oid"
                ).scalar_one()
                == original_oid
            )
            assert conn.exec_driver_sql(
                "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class "
                "WHERE oid='sdd_catalog.dataset_query_jobs'::regclass"
            ).scalar_one()
        saved = QueryJobs(db).submit(
            "tenant-a", "alice", "reader", {"sql": "SELECT * FROM original"}, "upgrade"
        )
        with db.transaction("tenant-b") as conn:
            assert (
                conn.execute(
                    select(schema.query_jobs).where(schema.query_jobs.c.id == saved["id"])
                ).first()
                is None
            )
    finally:
        db.engine.dispose()


def test_cancelling_job_keeps_lease_while_parallel_evidence_settles(population, monkeypatch):
    monkeypatch.setenv("SDD_JEV_CONCURRENCY", "2")
    db = population["app"]
    dataset = Catalog(db).create(
        "tenant-a",
        "notes",
        [{"id": n, "body": f"Please review record {n}"} for n in range(8)],
        primary_key=["id"],
    )

    class Model:
        model = "fixture-job-evidence-v1"

        def __init__(self):
            self.calls = 0
            self.lock, self.started, self.release = Lock(), Event(), Event()

        def ask(self, tenant, state, questions):
            with self.lock:
                self.calls += 1
                if self.calls == 2:
                    self.started.set()
            assert self.release.wait(15)
            return {
                "model": self.model,
                "answers": {key: {"type": "noul", "noul": 0.95} for key in questions},
                "usage": {},
            }

    model = Model()
    jobs = QueryJobs(db)
    saved = submit(
        population,
        "parallel-evidence",
        "SELECT id FROM notes WHERE SEMANTIC(body, 'Requests review')",
    )
    worker = QueryWorker(db, model, lease_seconds=5, poll_seconds=0.05)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(worker.work_one, "tenant-a")
            try:
                assert model.started.wait(5)
                assert jobs.cancel("tenant-a", "alice", saved["id"])["job_state"] == "CANCELLING"
                assert model.release.wait(5.2) is False
                assert jobs.get("tenant-a", "alice", saved["id"])["job_state"] == "CANCELLING"
            finally:
                model.release.set()
            assert pending.result(timeout=5)
        assert jobs.get("tenant-a", "alice", saved["id"])["job_state"] == "CANCELLED"
        assert model.calls == 2
        with db.transaction("tenant-a") as conn:
            assert (
                len(
                    conn.execute(
                        select(schema.evidence).where(
                            schema.evidence.c.dataset_id == dataset["id"],
                            schema.evidence.c.state == "succeeded",
                        )
                    ).all()
                )
                == 2
            )
        submit(
            population,
            "parallel-retry",
            "SELECT id FROM notes WHERE SEMANTIC(body, 'Requests review')",
        )
        assert worker.work_one("tenant-a")
        assert model.calls == 8
    finally:
        model.release.set()
        worker.close()


def test_status_page_does_not_load_retained_preview_payloads(population):
    import tracemalloc

    jobs = QueryJobs(population["app"])
    saved = submit(population, "large-preview")
    claim = jobs.claim("tenant-a")
    assert claim["id"] == saved["id"]
    assert jobs.finish(
        claim,
        {
            "mutation_preview": True,
            "manifest": {"complete": True},
            "changes_sample": [{"body": "x" * 3_000_000}],
        },
    )
    tracemalloc.start()
    try:
        page = jobs.recent("tenant-a", "alice", 50)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    match = next(item for item in page["items"] if item["id"] == saved["id"])
    assert match["operation_state"] == "AWAITING_REVIEW" and match["result"] is None
    assert peak < 1_000_000
