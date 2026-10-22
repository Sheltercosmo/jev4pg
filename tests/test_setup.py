import json
import os

import pytest
from sqlalchemy.engine import make_url

from sdd.setup import write_profile


def test_profile_separates_credentials_and_preserves_tls(tmp_path):
    path = write_profile(
        tmp_path / "profile with spaces",
        "postgresql://owner:secret@db.example/warehouse?sslmode=verify-full&sslrootcert=%2Fcerts%2Fca.pem",
        "分析员",
        "p@ssword:/?#' with spaces",
        "provider-secret",
    )
    runtime = make_url((path / ".secrets/runtime-url").read_text().strip())
    assert runtime.username == "分析员"
    assert runtime.password == "p@ssword:/?#' with spaces"
    assert runtime.query["sslmode"] == "verify-full"
    assert runtime.query["sslrootcert"] == "/certs/ca.pem"
    environment = (path / ".env").read_text(encoding="utf-8")
    assert "ADMIN" not in environment
    assert "secret" not in environment.replace(".secrets", "")
    tokens = json.loads((path / ".secrets/api-tokens.json").read_text())
    assert len(next(iter(tokens))) >= 32
    assert next(iter(tokens.values()))["tenant"] == "demo"
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o700
        assert (path / ".secrets/admin-url").stat().st_mode & 0o777 == 0o600


def test_rerun_preserves_the_profile(tmp_path):
    path = write_profile(tmp_path / "profile", "postgresql://owner@localhost/db", "app", "password")
    before = (path / ".secrets/runtime-url").read_bytes()
    with pytest.raises(FileExistsError):
        write_profile(path, "postgresql://owner@localhost/db", "other", "different")
    assert (path / ".secrets/runtime-url").read_bytes() == before


@pytest.mark.parametrize(
    "url,user,password",
    [
        ("sqlite:///test", "app", "pw"),
        ("postgresql://owner@remote/db", "app", "pw"),
        ("postgresql://owner@localhost/db", "owner", "pw"),
        ("postgresql://owner@localhost/db?user=owner", "app", "pw"),
        ("postgresql://owner@localhost/db?options=-crole%3Downer", "app", "pw"),
        ("postgresql://owner@localhost/db", "app\nOTHER=1", "pw"),
        ("postgresql://owner@localhost/db", "app", " pw "),
    ],
)
def test_invalid_profile_has_no_files(tmp_path, url, user, password):
    path = tmp_path / "profile"
    with pytest.raises(ValueError):
        write_profile(path, url, user, password)
    assert not path.exists()


def test_cli_portable_preflight_and_explicit_environment(tmp_path, monkeypatch, capsys):
    from sdd import bootstrap, cli

    path = tmp_path / "admin.env"
    path.write_text("SDD_ADMIN_DATABASE_URL=postgresql://owner@localhost/test\nSDD_DB_USER=app\n")
    monkeypatch.chdir(tmp_path)
    for key in ("SDD_ADMIN_DATABASE_URL", "SDD_ADMIN_DATABASE_URL_FILE", "SDD_DB_USER"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "sys.argv",
        ["jev4pg", "--env-file", str(path), "migrate", "--check", "--sql-interface", "portable"],
    )

    def check(url, role, mode):
        assert url == "postgresql://owner@localhost/test" and role == "app" and mode == "portable"
        return {"status": "ready"}

    monkeypatch.setattr(bootstrap, "check_migration", check)
    cli.main()
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
