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


def test_structured_tls_settings_and_explicit_url_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL_FILE", raising=False)
    monkeypatch.delenv("SDD_ADMIN_DATABASE_URL", raising=False)
    monkeypatch.delenv("SDD_ADMIN_DATABASE_URL_FILE", raising=False)
    for prefix in ("SDD_DB_", "SDD_ADMIN_DB_"):
        monkeypatch.setenv(prefix + "USER", "restricted")
        monkeypatch.setenv(prefix + "PASSWORD", "a password with @ and / and spaces")
        monkeypatch.delenv(prefix + "PASSWORD_FILE", raising=False)
    monkeypatch.setenv("SDD_DB_SSLMODE", "verify-full")
    monkeypatch.setenv("SDD_DB_SSLROOTCERT", str(tmp_path / "company root.crt"))
    monkeypatch.setenv("SDD_DB_CONNECT_TIMEOUT", "7")
    monkeypatch.setenv("SDD_DB_APPLICATION_NAME", "analysis-workspace")
    for admin in (False, True):
        url = database_url(admin=admin)
        assert url.query["sslmode"] == "verify-full"
        assert url.query["sslrootcert"].endswith("company root.crt")
        assert url.query["connect_timeout"] == "7"
        assert url.query["application_name"] == "analysis-workspace"
    explicit = "postgresql+psycopg://runtime@db/warehouse?sslmode=require"
    monkeypatch.setenv("DATABASE_URL", explicit)
    assert database_url() == explicit


@pytest.mark.parametrize(
    "setting,value",
    [
        ("SDD_DB_SSLMODE", "verify-everything"),
        ("SDD_DB_CONNECT_TIMEOUT", "0"),
        ("SDD_DB_CONNECT_TIMEOUT", "nan"),
    ],
)
def test_invalid_structured_connection_options_fail_early(monkeypatch, setting, value):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL_FILE", raising=False)
    monkeypatch.delenv("SDD_DB_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("SDD_DB_USER", "runtime")
    monkeypatch.setenv("SDD_DB_PASSWORD", "example")
    monkeypatch.setenv(setting, value)
    with pytest.raises(ValueError, match=setting):
        database_url()


@pytest.mark.parametrize(
    "setting,value",
    [
        ("SDD_DB_POOL_SIZE", "0"),
        ("SDD_DB_POOL_SIZE", "1.5"),
        ("SDD_DB_MAX_OVERFLOW", "-1"),
        ("SDD_DB_GUARD_POOL_SIZE", "0"),
        ("SDD_DB_POOL_TIMEOUT", "nan"),
        ("SDD_DB_POOL_TIMEOUT", "inf"),
        ("SDD_DB_POOL_TIMEOUT", "0"),
        ("SDD_DB_POOL_RECYCLE", "-1"),
    ],
)
def test_unbounded_or_invalid_pool_settings_are_rejected(monkeypatch, setting, value):
    monkeypatch.setenv(setting, value)
    with pytest.raises(ValueError, match=setting):
        Database("postgresql+psycopg://runtime@localhost/unused")


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
    assert result.stdout.strip() == "jev4pg " + __version__
