import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from deploy.configure import configure
from sdd.api import create_app
from sdd.config import api_tokens, database_url, load_secrets
from sdd.db import Database
from sdd.execution import Executor
from sdd import __version__


def test_production_requires_real_token_mapping(monkeypatch):
    monkeypatch.setenv("SDD_ENV", "production")
    monkeypatch.delenv("SDD_API_TOKENS_FILE", raising=False)
    for tokens in (
        {},
        [],
        {"short": {"tenant": "a", "name": "owner", "role": "reviewer"}},
        {"x" * 32: {"tenant": "a", "name": "owner", "role": "admin"}},
    ):
        monkeypatch.setenv("SDD_API_TOKENS", json.dumps(tokens))
        with pytest.raises(ValueError):
            api_tokens()
    monkeypatch.setenv(
        "SDD_API_TOKENS", json.dumps({"x" * 32: {"tenant": "a", "name": "owner", "role": "reader"}})
    )
    assert len(api_tokens()) == 1


def test_secret_files_override_environment_and_passwords_are_encoded(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL_FILE", raising=False)
    password = "complex@password:/?#'with spaces" * 2
    source = tmp_path / "password"
    source.write_text(password)
    monkeypatch.setenv("SDD_DB_USER", "runtime")
    monkeypatch.setenv("SDD_DB_PASSWORD_FILE", str(source))
    monkeypatch.setenv("SDD_DB_HOST", "db.internal")
    url = database_url()
    assert url.password == password and url.host == "db.internal"
    monkeypatch.setenv("SDD_JEV_API_KEY", "old-value")
    monkeypatch.setenv("SDD_JEV_API_KEY_FILE", str(source))
    load_secrets()
    import os

    assert os.environ["SDD_JEV_API_KEY"] == password


def test_secret_generation_is_repeatable_without_rotation(tmp_path):
    directory = configure(tmp_path / "secrets", prompt=False)
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    configure(directory, prompt=False)
    assert before == {path.name: path.read_bytes() for path in directory.iterdir()}
    assert len(json.loads(before["api_tokens.json"])) == 1
    assert before["postgres_password"] != before["app_password"]


def test_native_configuration_adds_a_separate_persistent_secret(tmp_path):
    directory = configure(tmp_path / "secrets", prompt=False)
    original = {path.name: path.read_bytes() for path in directory.iterdir()}
    configure(directory, prompt=False, native=True)
    native = (directory / "native_registry_password").read_bytes()
    assert len(native.strip()) >= 24 and native not in original.values()
    configure(directory, prompt=False, native=True)
    assert (directory / "native_registry_password").read_bytes() == native
    assert all((directory / name).read_bytes() == data for name, data in original.items())


def test_registry_requires_native_installation():
    from sdd.bootstrap import migrate

    with pytest.raises(ValueError, match="native-interface"):
        migrate("postgresql+psycopg://unused", native_registry=True)


def test_readiness_fails_without_exposing_connection_details(monkeypatch):
    db = Database("sqlite:///:memory:")
    db.initialize()
    client = TestClient(create_app(Executor(db, {}), tokens={}))
    assert client.get("/health").json() == {"status": "ok", "version": __version__}
    assert client.get("/openapi.json").json()["info"]["version"] == __version__
    assert client.get("/ready").status_code == 200
    monkeypatch.setenv("SDD_ENV", "production")
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": "Database is not ready"}
    db.engine.dispose()


def test_version_is_available_without_database_configuration():
    result = subprocess.run(
        [sys.executable, "-m", "sdd.cli", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "jevsd-pg " + __version__
