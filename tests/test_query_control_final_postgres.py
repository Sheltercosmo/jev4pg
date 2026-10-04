"""Connection isolation cases first exercised after the query-control runtime freeze."""

from concurrent.futures import ThreadPoolExecutor
import os
from time import monotonic, sleep

import pytest
from sqlalchemy import text

from test_deployment_postgres import installation as installation
from test_query_control_postgres import EXPENSIVE, population as population
from sdd.db import Database
from sdd.generic.catalog import Catalog
from sdd.generic.source_catalog import source_transaction
from sdd.generic.sql import SQLService
from sdd.query_control import QueryControl, QueryInterrupted


pytestmark = pytest.mark.skipif(
    not os.getenv("SDD_TEST_ADMIN_URL"), reason="Dedicated PostgreSQL server required"
)


def backend(db):
    with db.transaction("tenant-a") as conn:
        return conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()


def activity(owner, pid):
    with owner.engine.connect() as conn:
        return (
            conn.execute(
                text("SELECT state, query, wait_event_type FROM pg_stat_activity WHERE pid=:pid"),
                {"pid": pid},
            )
            .mappings()
            .one()
        )


def wait_active(owner, pid, *, lock=False):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        record = activity(owner, pid)
        if record["state"] == "active" and (
            record["wait_event_type"] == "Lock"
            if lock
            else record["query"].startswith("FETCH ") or "_sdd_result" in record["query"]
        ):
            return
        sleep(0.01)
    raise AssertionError("Expected controlled PostgreSQL execution to become active")


def test_finished_handle_cannot_cancel_next_pool_borrower(population, monkeypatch):
    monkeypatch.setenv("SDD_DB_POOL_SIZE", "1")
    monkeypatch.setenv("SDD_DB_MAX_OVERFLOW", "0")
    db = Database(population["app"].engine.url)
    service, finished, current = SQLService(db), QueryControl(), QueryControl()
    try:
        assert service.execute("tenant-a", "SELECT id FROM events WHERE id=1", control=finished)[
            "result"
        ] == [{"id": 1}]
        pid = backend(db)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(service.execute, "tenant-a", EXPENSIVE, control=current)
            try:
                wait_active(population["owner"], pid)
                assert not finished.cancel()
                assert activity(population["owner"], pid)["state"] == "active"
                assert not pending.done()
            finally:
                current.cancel()
            with pytest.raises(QueryInterrupted):
                pending.result(timeout=3)
        assert backend(db) == pid
    finally:
        db.engine.dispose()


def test_two_active_queries_have_independent_cancellation(population, monkeypatch):
    monkeypatch.setenv("SDD_DB_POOL_SIZE", "1")
    monkeypatch.setenv("SDD_DB_MAX_OVERFLOW", "0")
    databases = [Database(population["app"].engine.url) for _ in range(2)]
    controls = [QueryControl(), QueryControl()]
    try:
        pids = [backend(db) for db in databases]
        assert len(set(pids)) == 2
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [
                pool.submit(SQLService(db).execute, "tenant-a", EXPENSIVE, control=control)
                for db, control in zip(databases, controls)
            ]
            try:
                for pid in pids:
                    wait_active(population["owner"], pid)
                assert controls[0].cancel()
                with pytest.raises(QueryInterrupted):
                    pending[0].result(timeout=3)
                assert activity(population["owner"], pids[1])["state"] == "active"
                assert not pending[1].done()
                assert controls[1].cancel()
                with pytest.raises(QueryInterrupted):
                    pending[1].result(timeout=3)
            finally:
                for control in controls:
                    control.cancel()
        assert [backend(db) for db in databases] == pids
    finally:
        for db in databases:
            db.engine.dispose()


def test_source_lock_wait_is_interruptible_with_chinese_identifiers(installation, monkeypatch):
    monkeypatch.setenv("SDD_DB_POOL_SIZE", "1")
    monkeypatch.setenv("SDD_DB_MAX_OVERFLOW", "0")
    db = Database(installation["app"].engine.url)
    role = installation["roles"][0]
    relation = '"业务来源"."工单"'
    with installation["owner"].engine.begin() as conn:
        conn.exec_driver_sql('CREATE SCHEMA "业务来源"')
        conn.exec_driver_sql(f'GRANT USAGE ON SCHEMA "业务来源" TO "{role}"')
        conn.exec_driver_sql(f'CREATE TABLE {relation} ("标识" integer PRIMARY KEY, "说明" text)')
        conn.exec_driver_sql(f"INSERT INTO {relation} VALUES (1, '待处理'), (2, NULL)")
        conn.exec_driver_sql(f'GRANT SELECT ON {relation} TO "{role}"')
    dataset = Catalog(db).attach("tenant-a", "工作记录", "业务来源", "工单")
    control = QueryControl()

    def read_source():
        with control.activate(), source_transaction(db, "tenant-a", [dataset]) as conn:
            return conn.exec_driver_sql(f"SELECT * FROM {relation}").all()

    try:
        pid = backend(db)
        with installation["owner"].engine.connect() as holder:
            holder.exec_driver_sql(f"LOCK TABLE {relation} IN ACCESS EXCLUSIVE MODE")
            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(read_source)
                    try:
                        wait_active(installation["owner"], pid, lock=True)
                        assert control.cancel()
                        with pytest.raises(QueryInterrupted) as error:
                            pending.result(timeout=2)
                        assert error.value.output_state == "NOT_EVALUATED"
                        assert error.value.operation_state == "CANCELLED"
                        assert holder.exec_driver_sql("SELECT 1").scalar_one() == 1
                    finally:
                        control.cancel()
            finally:
                holder.rollback()
        assert backend(db) == pid
        assert SQLService(db).execute(
            "tenant-a", 'SELECT "标识", "说明" FROM "工作记录" ORDER BY "标识"'
        )["result"] == [{"标识": 1, "说明": "待处理"}, {"标识": 2, "说明": None}]
    finally:
        db.engine.dispose()
