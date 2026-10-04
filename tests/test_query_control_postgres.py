from concurrent.futures import ThreadPoolExecutor
import os
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from test_deployment_postgres import installation as installation
from sdd.api import create_app
from sdd.db import Database
from sdd.execution import Executor
from sdd.generic import schema
from sdd.generic.catalog import Catalog
from sdd.generic.sql import SQLService
from sdd.query_control import QueryControl, QueryInterrupted


pytestmark = pytest.mark.skipif(
    not os.getenv("SDD_TEST_ADMIN_URL"), reason="Dedicated PostgreSQL server required"
)


@pytest.fixture(scope="module")
def population(installation):
    dataset = Catalog(installation["app"]).create(
        "tenant-a", "events", [], primary_key=["id"], columns=[{"name": "id", "type": "integer"}]
    )
    with installation["owner"].engine.begin() as conn:
        conn.exec_driver_sql(
            f'INSERT INTO sdd_data."{dataset["table_name"]}" SELECT n FROM generate_series(1,100000) n'
        )
    return installation


EXPENSIVE = "SELECT COUNT(*) AS n FROM events a JOIN events b ON a.id <> b.id"


def running_query(owner, role):
    until = monotonic() + 5
    while monotonic() < until:
        with owner.engine.connect() as conn:
            pid = conn.execute(
                text(
                    "SELECT pid FROM pg_stat_activity WHERE usename=:role AND state='active' "
                    "AND (query LIKE 'FETCH %' OR query LIKE 'SELECT * FROM (%_sdd_result%')"
                ),
                {"role": role},
            ).scalar()
        if pid:
            return pid
        sleep(0.01)
    raise AssertionError("Expected expensive query to be active on PostgreSQL")


def test_cancel_active_backend_and_reuse_same_connection(population, monkeypatch):
    monkeypatch.setenv("SDD_DB_POOL_SIZE", "1")
    monkeypatch.setenv("SDD_DB_MAX_OVERFLOW", "0")
    db = Database(population["app"].engine.url)
    service, control = SQLService(db), QueryControl()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(service.execute, "tenant-a", EXPENSIVE, control=control)
            pid = running_query(population["owner"], population["roles"][0])
            assert SQLService(population["app"]).execute(
                "tenant-a", "SELECT id FROM events WHERE id=42"
            )["result"] == [{"id": 42}]
            started = monotonic()
            assert control.cancel()
            with pytest.raises(QueryInterrupted) as error:
                task.result(timeout=3)
            assert monotonic() - started < 3
            assert error.value.operation_state == "CANCELLED"
        with db.transaction("tenant-a") as conn:
            assert conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one() == pid
            assert (
                conn.execute(
                    select(func.count())
                    .select_from(schema.runs)
                    .where(schema.runs.c.logical_sql == EXPENSIVE)
                ).scalar_one()
                == 0
            )
        assert service.execute("tenant-a", "SELECT COUNT(*) AS n FROM events")["result"] == [
            {"n": 100000}
        ]
    finally:
        db.engine.dispose()


def test_deadline_interrupts_sql_and_api_reports_missing_result(population):
    client = TestClient(
        create_app(
            Executor(population["app"], {}),
            tokens={"test-reader": {"tenant": "tenant-a", "name": "reader", "role": "reader"}},
        )
    )
    started = monotonic()
    response = client.post(
        "/data/sql",
        json={"sql": EXPENSIVE, "timeout_seconds": 0.25},
        headers={"Authorization": "Bearer test-reader"},
    )
    assert monotonic() - started < 3
    assert response.status_code == 408, response.text
    outcome = response.json()
    history_id = outcome.pop("history_id")
    assert outcome == {
        "detail": "Query deadline exceeded",
        "output_state": "NOT_EVALUATED",
        "operation_state": "TIMED_OUT",
        "result": None,
    }
    saved = client.get(
        f"/query-history/{history_id}", headers={"Authorization": "Bearer test-reader"}
    ).json()
    assert saved["status"] == "timed_out" and saved["input"]["text"] == EXPENSIVE
    assert saved["output"]["executed"] is False


def test_failed_cancel_transport_discards_connection(population, monkeypatch):
    import psycopg

    monkeypatch.setenv("SDD_DB_POOL_SIZE", "1")
    monkeypatch.setenv("SDD_DB_MAX_OVERFLOW", "0")
    db = Database(population["app"].engine.url)
    control = QueryControl(0.1)
    before = None

    def unavailable(*args, **kwargs):
        raise psycopg.OperationalError("private transport failure")

    monkeypatch.setattr(psycopg.Connection, "cancel_safe", unavailable)
    try:
        with pytest.raises(QueryInterrupted), control.activate():
            with db.transaction("tenant-a", interruptible=True) as conn:
                before = conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
                conn.exec_driver_sql("SELECT pg_sleep(0.25)")
        assert control.cancel_errors == ["OperationalError"]
        with db.transaction("tenant-a") as conn:
            assert conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one() != before
            assert conn.exec_driver_sql("SELECT 1").scalar_one() == 1
    finally:
        db.engine.dispose()
