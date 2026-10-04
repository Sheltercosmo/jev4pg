"""New lifecycle cases first exercised after the durable-query runtime freeze."""

import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import select

from test_deployment_postgres import installation as installation
from sdd.generic import schema
from sdd.generic.catalog import Catalog
from sdd.generic.history import QueryHistory
from sdd.generic.query_jobs import QueryJobs
from sdd.generic.sql import SQLService
from sdd.query_worker import QueryWorker


pytestmark = pytest.mark.skipif(
    not os.getenv("SDD_TEST_ADMIN_URL"), reason="Dedicated PostgreSQL server required"
)


def test_cli_worker_visits_separate_tenants_with_chinese_catalogs(installation, tmp_path):
    db, jobs = installation["app"], QueryJobs(installation["app"])
    saved = []
    for tenant, value in (("租户甲", "需要回访"), ("租户乙", "已经解决")):
        Catalog(db).create(tenant, "工单", [{"编号": 1, "说明": value}], primary_key=["编号"])
        saved.append(
            (
                tenant,
                value,
                jobs.submit(
                    tenant,
                    "分析员",
                    "reader",
                    {"sql": 'SELECT "说明" FROM "工单" WHERE "编号"=1'},
                    "首个任务",
                ),
            )
        )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SDD_", "DATABASE_URL", "TYPESAFE_", "OPENAI_"))
    }
    environment.update(
        DATABASE_URL=db.engine.url.render_as_string(hide_password=False),
        SDD_ENV="production",
        PYTHONPATH=str(Path(__file__).resolve().parents[1]),
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "sdd.cli",
            "query-worker",
            "--tenant",
            "租户甲",
            "--tenant",
            "租户乙",
            "--concurrency",
            "1",
            "--once",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert completed.returncode == 0
    for tenant, value, job in saved:
        result = jobs.get(tenant, "分析员", job["id"])
        assert result["job_state"] == "SUCCEEDED"
        assert result["result"]["result"] == [{"说明": value}]
        assert QueryHistory(db).get(tenant, "分析员", job["id"])["status"] == "complete"


def test_job_deadline_has_no_published_result_and_keeps_editable_request(installation):
    db = installation["app"]
    dataset = Catalog(db).create(
        "tenant-a",
        "samples",
        [],
        primary_key=["number"],
        columns=[{"name": "number", "type": "integer"}],
    )
    with installation["owner"].engine.begin() as conn:
        conn.exec_driver_sql(
            f'INSERT INTO sdd_data."{dataset["table_name"]}" '
            "SELECT n FROM generate_series(1,100000) n"
        )
    query = "SELECT COUNT(*) FROM samples a JOIN samples b ON a.number<>b.number"
    jobs = QueryJobs(db)
    job = jobs.submit(
        "tenant-a", "reviewer", "reviewer", {"sql": query, "timeout_seconds": 0.2}, "deadline"
    )
    worker = QueryWorker(db)
    try:
        assert worker.work_one("tenant-a")
    finally:
        worker.close()
    result = jobs.get("tenant-a", "reviewer", job["id"])
    assert result["job_state"] == result["operation_state"] == "TIMED_OUT"
    assert result["output_state"] == "NOT_EVALUATED" and result["result"] is None
    record = QueryHistory(db).get("tenant-a", "reviewer", job["id"])
    assert record["status"] == "timed_out" and record["input"]["text"] == query


def test_reused_attachment_name_cannot_redirect_a_queued_query(installation):
    db, role = installation["app"], installation["roles"][0]
    with installation["owner"].engine.begin() as conn:
        conn.exec_driver_sql("CREATE SCHEMA application_sources")
        conn.exec_driver_sql(f'GRANT USAGE ON SCHEMA application_sources TO "{role}"')
        conn.exec_driver_sql(
            "CREATE TABLE application_sources.original (id integer PRIMARY KEY, value text)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE application_sources.replacement (id integer PRIMARY KEY, value text)"
        )
        conn.exec_driver_sql("INSERT INTO application_sources.original VALUES (1,'original')")
        conn.exec_driver_sql("INSERT INTO application_sources.replacement VALUES (1,'different')")
        conn.exec_driver_sql(
            f'GRANT SELECT ON ALL TABLES IN SCHEMA application_sources TO "{role}"'
        )
    catalog, jobs = Catalog(db), QueryJobs(db)
    source = catalog.attach("tenant-a", "current_records", "application_sources", "original")
    job = jobs.submit(
        "tenant-a", "alice", "reader", {"sql": "SELECT * FROM current_records"}, "binding"
    )
    catalog.detach("tenant-a", source["id"])
    catalog.attach("tenant-a", "current_records", "application_sources", "replacement")
    worker = QueryWorker(db)
    try:
        assert worker.work_one("tenant-a")
    finally:
        worker.close()
    outcome = jobs.get("tenant-a", "alice", job["id"])
    assert outcome["job_state"] == "FAILED" and outcome["result"] is None
    with db.transaction("tenant-a") as conn:
        assert (
            conn.execute(
                select(schema.runs).where(
                    schema.runs.c.logical_sql == "SELECT * FROM current_records"
                )
            ).first()
            is None
        )


def test_managed_deletion_fences_a_result_already_computed_by_a_worker(installation):
    db = installation["app"]
    Catalog(db).create(
        "tenant-a",
        "contacts",
        [{"id": 1, "note": "retained"}, {"id": 2, "note": "remove"}],
        primary_key=["id"],
    )
    jobs, service = QueryJobs(db), SQLService(db)
    job = jobs.submit(
        "tenant-a", "owner", "reader", {"sql": "SELECT * FROM contacts"}, "deletion-race"
    )
    claimed = jobs.claim("tenant-a")
    assert claimed["id"] == job["id"]
    unpublished = service.execute("tenant-a", "SELECT * FROM contacts")
    preview = service.execute("tenant-a", "DELETE FROM contacts WHERE id=2", actor="owner")
    service.commit("tenant-a", preview["preview_token"], "owner")
    assert not jobs.finish(claimed, unpublished)
    result = jobs.get("tenant-a", "owner", job["id"])
    assert result["job_state"] == "REDACTED" and result["result"] is None
    assert QueryHistory(db).get("tenant-a", "owner", job["id"])["input"] == {}
