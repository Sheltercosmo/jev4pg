from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest
from sqlalchemy import func, select

from sdd.db import Database
from sdd.generic import schema
from sdd.generic.catalog import Catalog
from sdd.generic.sql import SQLService
from sdd.query_control import QueryControl, QueryInterrupted


@pytest.fixture
def source(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "query-control.db"))
    db.initialize()
    dataset = Catalog(db).create(
        "a",
        "messages",
        [{"id": n, "body": f"Please check record {n}"} for n in range(8)],
        primary_key=["id"],
    )
    yield db, dataset
    db.engine.dispose()


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "5", 3601])
def test_invalid_timeout(timeout):
    with pytest.raises(ValueError):
        QueryControl(timeout)


def test_cancel_before_start_and_handle_cannot_be_reused(source):
    db, _ = source
    control = QueryControl()
    assert control.cancel()
    with pytest.raises(QueryInterrupted) as error:
        SQLService(db).execute("a", "SELECT id FROM messages", control=control)
    assert error.value.output_state == "NOT_EVALUATED"
    assert error.value.operation_state == "CANCELLED"
    assert not control.cancel()
    with pytest.raises(ValueError, match="new query control"):
        SQLService(db).execute("a", "SELECT id FROM messages", control=control)


def test_successful_control_detaches_and_preview_still_requires_commit(source):
    db, _ = source
    service = SQLService(db)
    control = QueryControl(5)
    result = service.execute("a", "SELECT COUNT(*) AS n FROM messages", control=control)
    assert result["result"] == [{"n": 8}]
    assert not control.cancel()
    preview = service.execute(
        "a", "UPDATE messages SET body='changed' WHERE id=1", actor="owner", control=QueryControl(5)
    )
    assert preview["mutation_preview"]
    assert service.execute("a", "SELECT body FROM messages WHERE id=1")["result"] == [
        {"body": "Please check record 1"}
    ]
    with QueryControl(5).activate(), pytest.raises(ValueError, match="not write commits"):
        service.commit("a", preview["preview_token"], "owner")
    assert service.commit("a", preview["preview_token"], "owner")["manifest"]["committed"]


def test_cancellation_drains_parallel_inference_and_retains_evidence(source, monkeypatch):
    monkeypatch.setenv("SDD_JEV_CONCURRENCY", "2")
    db, _ = source

    class Model:
        model = "fixture-control-v1"

        def __init__(self):
            self.calls = 0
            self.lock, self.started, self.release = Lock(), Event(), Event()

        def ask(self, tenant, state, questions):
            with self.lock:
                self.calls += 1
                if self.calls == 2:
                    self.started.set()
            assert self.release.wait(10)
            return {
                "model": self.model,
                "answers": {key: {"type": "noul", "noul": 0.95} for key in questions},
                "usage": {"input_tokens": 20, "output_tokens": 0},
            }

    model = Model()
    service, control = SQLService(db, model), QueryControl()
    query = "SELECT id FROM messages WHERE SEMANTIC(body, 'Requests action') ORDER BY id"
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(service.execute, "a", query, control=control)
        try:
            assert model.started.wait(5)
            assert control.cancel()
        finally:
            model.release.set()
        with pytest.raises(QueryInterrupted):
            task.result(timeout=10)
    assert model.calls == 2
    with db.transaction("a") as conn:
        assert conn.execute(select(func.count()).select_from(schema.runs)).scalar_one() == 0
        assert (
            conn.execute(
                select(func.count())
                .select_from(schema.evidence)
                .where(schema.evidence.c.state == "succeeded")
            ).scalar_one()
            == 2
        )
    result = service.execute("a", query)
    assert result["result"] == [{"id": n} for n in range(8)]
    assert model.calls == 8


def test_deadline_exception_has_no_result_or_source_data():
    control = QueryControl(0.03)
    with pytest.raises(QueryInterrupted) as error, control.activate():
        assert control._finished.wait(0.1) is False
        control.check()
    assert error.value.operation_state == "TIMED_OUT"
    assert str(error.value) == "Query deadline exceeded"
