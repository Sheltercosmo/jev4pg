"""Workspace recovery contracts first checked after the controller/runtime freeze."""

import os

import pytest
from fastapi.testclient import TestClient

from test_deployment_postgres import installation as installation
from sdd.api import create_app
from sdd.execution import Executor
from sdd.generic.catalog import Catalog
from sdd.query_worker import QueryWorker


pytestmark = pytest.mark.skipif(
    not os.getenv("SDD_TEST_ADMIN_URL"), reason="Dedicated PostgreSQL server required"
)


def workspace(installation):
    return TestClient(
        create_app(
            Executor(installation["app"], {}),
            tokens={
                "owner": {"tenant": "tenant-a", "name": "分析员", "role": "reviewer"},
                "peer": {"tenant": "tenant-a", "name": "同事", "role": "reviewer"},
            },
        )
    )


HEADERS = {"Authorization": "Bearer owner"}


def work(installation):
    worker = QueryWorker(installation["app"])
    try:
        assert worker.work_one("tenant-a")
    finally:
        worker.close()


def test_history_recovers_chinese_query_with_null_and_exact_large_identifiers(installation):
    Catalog(installation["app"]).create(
        "tenant-a",
        "运输批次",
        [
            {"编号": 9007199254741001, "备注": "待回访"},
            {"编号": 9007199254741003, "备注": None},
        ],
        primary_key=["编号"],
    )
    client = workspace(installation)
    query = 'SELECT "编号", "备注" FROM "运输批次" ORDER BY "编号"'
    body = {"sql": query, "idempotency_key": "运输结果"}
    submitted = client.post("/data/query-jobs", headers=HEADERS, json=body)
    assert submitted.status_code == 202, submitted.text
    identity = submitted.json()["id"]
    pending = client.get(f"/query-history/{identity}", headers=HEADERS).json()
    assert pending["query_job_id"] == identity and pending["status"] == "queued"
    work(installation)

    reopened = workspace(installation)
    history = reopened.get(f"/query-history/{identity}", headers=HEADERS).json()
    result = reopened.get(f"/data/query-jobs/{history['query_job_id']}", headers=HEADERS).json()
    expected = [
        {"编号": "9007199254741001", "备注": "待回访"},
        {"编号": "9007199254741003", "备注": None},
    ]
    assert result["result"]["result"] == history["output"]["result"] == expected
    assert reopened.post("/data/query-jobs", headers=HEADERS, json=body).json()["id"] == identity
    assert (
        reopened.get(
            f"/query-history/{identity}", headers={"Authorization": "Bearer peer"}
        ).status_code
        == 400
    )
    assert len(reopened.get("/data/query-jobs", headers=HEADERS).json()["items"]) == 1


def test_recovered_write_preview_and_revised_request_keep_separate_history(installation):
    Catalog(installation["app"]).create(
        "tenant-a",
        "DispatchNotes",
        [
            {"key": 1, "note": "waiting"},
            {"key": 2, "note": None},
        ],
        primary_key=["key"],
    )
    client = workspace(installation)
    original = client.post(
        "/data/query-jobs",
        headers=HEADERS,
        json={
            "sql": "UPDATE \"DispatchNotes\" SET note='reviewed' WHERE key=1",
            "idempotency_key": "first-preview",
        },
    ).json()
    work(installation)
    history = client.get(f"/query-history/{original['id']}", headers=HEADERS).json()
    recovered = client.get(f"/data/query-jobs/{history['query_job_id']}", headers=HEADERS).json()
    assert history["status"] == "preview"
    assert recovered["operation_state"] == "AWAITING_REVIEW"
    assert recovered["result"]["preview_token"]
    revision = client.post(
        "/data/query-jobs",
        headers=HEADERS,
        json={
            "sql": 'SELECT note FROM "DispatchNotes" WHERE key=1',
            "idempotency_key": "edited-read",
            "parent_history_id": original["id"],
        },
    ).json()
    work(installation)
    edited = client.get(f"/query-history/{revision['id']}", headers=HEADERS).json()
    assert edited["parent_id"] == original["id"]
    assert edited["output"]["result"] == [{"note": "waiting"}]
    assert client.get(f"/query-history/{original['id']}", headers=HEADERS).json() == history
