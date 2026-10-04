import json

import httpx
from fastapi.testclient import TestClient

from examples.activity_app import app as example


def test_example_backend_identity_exact_filters_and_error_handling(monkeypatch):
    monkeypatch.setenv("SDD_API_URL", "http://database.local")
    monkeypatch.setenv("SDD_API_TOKEN", "example-reader-only")
    monkeypatch.setenv("SDD_ACTIVITY_DATASET", "source-123")
    calls = []

    def handle(request):
        calls.append((request, json.loads(request.content)))
        return httpx.Response(
            200 if len(calls) == 1 else 403,
            json={
                "result": [],
                "next_after": None,
                "returned_rows": 0,
                "detail": "private diagnostic",
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        example.httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handle)),
    )
    with TestClient(example.app) as client:
        response = client.get(
            "/api/activity?category=warning&minimum=123.4567890123456789&after=9007199254740993"
        )
        assert response.status_code == 200
        request, body = calls[0]
        assert request.headers["authorization"] == "Bearer example-reader-only"
        assert body["limit"] == 50 and body["after"] == ["9007199254740993"]
        assert body["filters"][1]["value"] == "123.4567890123456789"
        assert body["columns"] == ["id", "category", "amount", "note", "created_at"]
        page = client.get("/")
        assert "example-reader-only" not in page.text
        assert "script-src 'self'" in page.headers["content-security-policy"]
        assert client.get("/api/activity?after=-1").status_code == 422
        result = client.get("/api/activity")
        assert result.status_code == 503 and "private diagnostic" not in result.text
