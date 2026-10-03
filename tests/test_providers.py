"""Provider contract tests use synthetic answers; they do not measure model quality."""

import copy
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import ModuleType

import httpx
import pytest
from fastapi.testclient import TestClient

from sdd.api import create_app
from sdd.config import runtime
from sdd.db import Database
from sdd.evaluators import DEFAULT_JEV_ENDPOINT, JevBackend, ProviderError
from sdd.execution import Executor
from sdd.generic.catalog import Catalog
from sdd.generic.jev import Decisions, choice, noul
from sdd.generic.planning_review import ReviewDecisions
from sdd.generic.sql import SQLService
from sdd.ledger import Ledger
from sdd.operators.service import OperatorService
from sdd.providers import PythonJevBackend, configured_backend

MODEL = "local-2026-10-01"


def reply(model, questions, probability=0.95):
    answers = {}
    for key, question in questions.items():
        kind = question["type"]
        if kind == "noul":
            answers[key] = {"type": kind, "noul": probability}
        elif kind == "choice":
            options = list(question["criteria"])
            answers[key] = {
                "type": kind,
                "choice": options[0],
                "probabilities": {k: float(k == options[0]) for k in options},
            }
        else:
            levels = question["criteria"]
            answers[key] = {
                "type": kind,
                "score": 1.0,
                "probabilities": {str(i): float(i == 1) for i in range(len(levels))},
                "legend": {str(i): v for i, v in enumerate(levels)},
            }
    return {"model": model, "answers": answers, "usage": {"input_tokens": 12}}


@pytest.fixture
def database(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "providers.db"))
    db.initialize()
    return db


def backend(endpoint="http://127.0.0.1:9100/v1/systemone", *, probability=0.95, revision=""):
    received = []

    def handle(request):
        payload = json.loads(request.content)
        received.append((request, payload))
        return httpx.Response(200, json=reply(payload["model"], payload["questions"], probability))

    client = httpx.Client(transport=httpx.MockTransport(handle))
    return JevBackend(client=client, endpoint=endpoint, revision=revision), received


@pytest.mark.parametrize("text", ["Work is complete.", "工作已完成。"])
def test_custom_endpoint_preserves_batched_primitives_and_unicode(database, text):
    provider, received = backend()
    decisions = Decisions(database, provider, MODEL)
    questions = {
        "truth": noul("Is the work complete?"),
        "category": choice("Select category", {"done": "Completed", "open": "Pending"}),
        "level": {"type": "score", "instructions": "Urgency", "criteria": ["Low", "High"]},
    }
    result = decisions.ask("tenant", {"text": text, "amount": 0}, questions)
    assert result["answers"]["truth"]["noul"] == 0.95
    assert result["answers"]["level"]["score"] == 1
    assert len(received) == 1
    request, payload = received[0]
    assert str(request.url) == provider.endpoint
    assert "authorization" not in request.headers
    assert payload == {"model": MODEL, "state": {"text": text, "amount": 0}, "questions": questions}


def test_custom_endpoint_never_inherits_typesafe_credentials():
    provider = configured_backend(
        {
            "TYPESAFE_API_KEY": "typesafe-secret",
            "SDD_JEV_ENDPOINT": "https://example.test/jev",
            "SDD_JEV_MODEL": MODEL,
        }
    )
    assert provider.api_key is None
    assert configured_backend({"TYPESAFE_API_KEY": "typesafe-secret"}).api_key == "typesafe-secret"
    configured = configured_backend(
        {
            "SDD_JEV_ENDPOINT": "https://example.test/jev",
            "SDD_JEV_MODEL": MODEL,
            "SDD_JEV_API_KEY": "third-party-secret",
        }
    )
    assert configured.api_key == "third-party-secret"


@pytest.mark.parametrize(
    "endpoint",
    [
        "file:///tmp/model",
        "ftp://localhost/model",
        "https://user:secret@example.test/jev",
        "https://example.test/jev?api_key=secret",
        "https://example.test/jev#secret",
    ],
)
def test_invalid_endpoint_is_rejected_without_echoing_credentials(endpoint):
    with pytest.raises(ValueError) as error:
        JevBackend(endpoint=endpoint)
    assert "secret" not in str(error.value)


def test_provider_identity_tracks_endpoint_model_and_revision():
    first, _ = backend()
    same, _ = backend()
    other, _ = backend("http://127.0.0.1:9101/v1/systemone")
    revised, _ = backend(revision="new-weights")
    assert first.cache_identity(MODEL) == same.cache_identity(MODEL)
    assert len({p.cache_identity(MODEL) for p in [first, other, revised]}) == 3
    assert first.cache_identity(MODEL) != first.cache_identity("another-model-v1")
    hosted = JevBackend("key")
    assert hosted.endpoint == DEFAULT_JEV_ENDPOINT
    assert hosted.cache_identity("jev-1.13.0") == "jev-1.13.0"
    hosted.api_key = "rotated-key"
    assert hosted.cache_identity("jev-1.13.0") == "jev-1.13.0"


def test_operator_cache_does_not_cross_providers(database):
    first, calls = backend()
    second, other_calls = backend("http://127.0.0.1:9101/v1/systemone", probability=0.05)
    args = {"state": "A record", "proposition": "The record matches"}
    a = OperatorService(database, Decisions(database, first, MODEL), "a", "reader")
    b = OperatorService(database, Decisions(database, second, MODEL), "a", "reader")
    assert a.call("NOUL", args)["value"]["0"]["value"] is True
    assert a.call("NOUL", args)["value"]["0"]["value"] is True
    assert b.call("NOUL", args)["value"]["0"]["value"] is False
    assert len(calls) == len(other_calls) == 1


def test_semantic_sql_cache_does_not_cross_providers(database):
    catalog = Catalog(database)
    catalog.create("a", "documents", [{"id": 1, "body": "A record"}], primary_key=["id"])
    first, calls = backend()
    second, other_calls = backend(revision="new-weights", probability=0.05)
    sql = "SELECT id FROM documents WHERE SEMANTIC(body, 'The record matches')"
    a = SQLService(database, Decisions(database, first, MODEL))
    b = SQLService(database, Decisions(database, second, MODEL))
    assert a.execute("a", sql)["result"] == [{"id": 1}]
    assert a.execute("a", sql)["result"] == [{"id": 1}]
    assert b.execute("a", sql)["result"] == []
    assert len(calls) == len(other_calls) == 1


def test_review_cache_is_bound_to_provider(database):
    provider, calls = backend()
    changed, new_calls = backend(revision="changed")
    question = {"q": noul("A proposition")}
    original = ReviewDecisions(Decisions(database, provider, MODEL))
    original.ask("a", {}, question)
    previous = {"batches": copy.deepcopy(original.batches)}
    ReviewDecisions(Decisions(database, provider, MODEL), previous).ask("a", {}, question)
    ReviewDecisions(Decisions(database, changed, MODEL), previous).ask("a", {}, question)
    assert len(calls) == len(new_calls) == 1


def test_local_factory_loads_without_api_key_and_batches(database, monkeypatch):
    received = []
    module = ModuleType("local_test_provider")

    def predict(**payload):
        received.append(payload)
        return reply(payload["model"], payload["questions"])

    module.create = lambda: predict
    monkeypatch.setitem(sys.modules, module.__name__, module)
    provider = configured_backend(
        {
            "SDD_JEV_TRANSPORT": "python",
            "SDD_JEV_ADAPTER": "local_test_provider:create",
            "SDD_JEV_MODEL": MODEL,
            "SDD_JEV_REVISION": "weights-and-prompt-v1",
        }
    )
    decisions = Decisions(database, provider, MODEL)
    result = decisions.ask("a", "来源文本", {"a": noul("First"), "b": noul("Second")})
    assert set(result["answers"]) == {"a", "b"}
    assert len(received) == 1 and received[0]["state"] == "来源文本"


def test_runtime_enables_unauthenticated_local_http(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + str(tmp_path / "runtime.db"))
    monkeypatch.setenv("SDD_JEV_ENDPOINT", "http://localhost:9100/v1/systemone")
    monkeypatch.setenv("SDD_JEV_MODEL", MODEL)
    monkeypatch.setenv("SDD_JEV_TRANSPORT", "systemone")
    monkeypatch.delenv("SDD_JEV_API_KEY", raising=False)
    db, executor = runtime()
    assert executor.workers.backends["jev"].endpoint == "http://localhost:9100/v1/systemone"
    assert executor.workers.backends["jev"].api_key is None
    db.engine.dispose()


@pytest.mark.parametrize(
    "settings",
    [
        {"SDD_JEV_TRANSPORT": "unknown"},
        {"SDD_JEV_ENDPOINT": "http://localhost:9100/jev"},
        {"SDD_JEV_TRANSPORT": "python"},
        {
            "SDD_JEV_TRANSPORT": "python",
            "SDD_JEV_ADAPTER": "missing:create",
            "SDD_JEV_MODEL": MODEL,
            "SDD_JEV_REVISION": "v1",
        },
    ],
)
def test_invalid_configuration_fails_before_any_inference(settings):
    with pytest.raises(ValueError):
        configured_backend(settings)


def test_local_failure_is_unexecuted_not_false(database):
    def fail(**kwargs):
        raise RuntimeError("private model path and secret")

    provider = PythonJevBackend(fail, adapter="local:create", revision="v1")
    service = OperatorService(database, Decisions(database, provider, MODEL), "a", "reader")
    result = service.call("NOUL", {"state": "A", "proposition": "B"}, {"retries": 0})
    assert result["output_state"] == "NOT_EVALUATED"
    assert result["operation_state"] == "FAILED"
    assert result["value"] is None
    assert "private model" not in json.dumps(result)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda result: result.update(model="another-model"),
        lambda result: result.update(answers={}),
        lambda result: result["answers"]["q"].update(noul=True),
        lambda result: result.update(usage={"input_tokens": -1}),
    ],
)
def test_local_answers_obey_the_same_strict_contract(database, mutation):
    def predict(**payload):
        result = reply(payload["model"], payload["questions"])
        mutation(result)
        return result

    decisions = Decisions(
        database, PythonJevBackend(predict, adapter="local:create", revision="v1"), MODEL
    )
    with pytest.raises(ProviderError, match="InvalidDecisionResponse"):
        decisions.ask("a", {}, {"q": noul("Question")})


def test_local_independent_requests_overlap(database, monkeypatch):
    monkeypatch.setenv("SDD_JEV_CONCURRENCY", "2")
    barrier = threading.Barrier(2, timeout=5)

    def predict(**payload):
        barrier.wait()
        return reply(payload["model"], payload["questions"])

    decisions = Decisions(
        database, PythonJevBackend(predict, adapter="local:create", revision="v1"), MODEL
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda state: decisions.ask("a", state, {"q": noul("Question")}),
                ["First", "第二个"],
            )
        )
    assert len(results) == 2


def test_retry_header_and_redirect_are_not_silent_fallbacks(database):
    for status, delay in [(429, "3"), (307, None)]:
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    status, headers={"Retry-After": delay} if delay else {}
                )
            )
        )
        decisions = Decisions(
            database, JevBackend(client=client, endpoint="http://localhost/jev"), MODEL
        )
        with pytest.raises(ProviderError) as error:
            decisions.ask("a", {}, {"q": noul("Question")})
        assert error.value.code == f"HTTP_{status}"
        assert error.value.retryable == (status == 429)
        assert error.value.retry_after == (3 if status == 429 else None)


def test_loopback_systemone_server_handles_real_http(database):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append(payload)
            body = json.dumps(reply(payload["model"], payload["questions"])).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/v1/systemone"
        decisions = Decisions(database, JevBackend(endpoint=endpoint), MODEL)
        result = decisions.ask("a", {"body": "完整的本地请求"}, {"q": noul("已完成")})
        assert result["answers"]["q"]["noul"] == 0.95
        assert received[0]["state"]["body"] == "完整的本地请求"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_legacy_evaluator_revision_is_bound_to_provider(database):
    first, _ = backend()
    second, _ = backend(revision="changed")
    evaluator = Ledger(database).evaluator(
        "a", "jev", MODEL, preprocessing=first.preprocessing(MODEL)
    )
    first.validate_evaluator(evaluator)
    with pytest.raises(ValueError, match="Provider configuration changed"):
        second.validate_evaluator(evaluator)


def test_approval_and_resume_cannot_cross_provider_config(database, monkeypatch):
    monkeypatch.setenv(
        "SDD_API_TOKENS",
        json.dumps({"test-token": {"tenant": "a", "name": "owner", "role": "reviewer"}}),
    )
    monkeypatch.setenv("SDD_JEV_MODEL", MODEL)
    monkeypatch.setenv("SDD_FEATURE_MAINTENANCE", "0")
    first, _ = backend()
    second, _ = backend(revision="changed")
    headers = {"Authorization": "Bearer test-token"}
    body = {
        "operator": "NOUL",
        "arguments": {"state": "A", "proposition": "B"},
        "limits": {"max_judgments": 0},
    }
    with TestClient(create_app(Executor(database, {"jev": first}))) as a:
        approval = a.post("/jev/approve", json=body, headers=headers).json()["approval_id"]
        original = a.post("/jev/call", json=body, headers=headers).json()
    with TestClient(create_app(Executor(database, {"jev": second}))) as b:
        response = b.post("/jev/call", json={**body, "approval_id": approval}, headers=headers)
        assert response.status_code == 400 and "Approval does not cover" in response.text
        response = b.post(f"/jev/runs/{original['run_id']}/resume", headers=headers)
        assert response.status_code == 400 and "Provider configuration changed" in response.text


def test_serve_does_not_load_a_local_model_before_app_start(monkeypatch):
    import uvicorn
    from sdd import cli

    calls = []
    monkeypatch.setattr(sys, "argv", ["jevsd-pg", "serve"])
    monkeypatch.setattr(cli, "runtime", lambda: pytest.fail("Model loaded outside the app factory"))
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    cli.main()
    assert calls[0][0] == ("sdd.api:create_app",)
    assert calls[0][1]["factory"] is True
