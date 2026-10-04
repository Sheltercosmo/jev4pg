import json
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from deploy.fixture_provider import Handler
from sdd.generic.semantic_types import SemanticSpec


def test_fixture_accepts_direct_and_application_questions_without_credentials():
    questions = {}
    expected = {}
    for definition, probability in (("true", 0.95), ("false", 0.05), ("unknown", 0.5)):
        questions[definition] = {"type": "noul", "instructions": definition}
        structured, _ = SemanticSpec("正文", definition).questions({}, "app_" + definition)
        questions.update(structured)
        expected[definition] = expected["app_" + definition] = probability

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_port}/v1/systemone",
            data=json.dumps({"model": "fixture", "questions": questions}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=5) as response:
            result = json.load(response)
        assert {key: answer["noul"] for key, answer in result["answers"].items()} == expected
        request.add_header("Authorization", "Bearer fixture-key")
        with pytest.raises(HTTPError) as error:
            urlopen(request, timeout=5)
        assert error.value.code == 400
        error.value.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
