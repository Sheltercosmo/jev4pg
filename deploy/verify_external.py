"""Test TLS and the application against a disposable external PostgreSQL 17 server."""

import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from sqlalchemy import URL

from sdd.bootstrap import migrate


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def verify(postgres_bin, openssl, full_suite=False):
    root = Path(__file__).resolve().parent.parent
    runtime = root / ".runtime"
    runtime.mkdir(exist_ok=True)
    pg = Path(postgres_bin).resolve()
    suffix = ".exe" if os.name == "nt" else ""
    windows = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

    def run(command, **kwargs):
        return subprocess.run(command, check=True, cwd=root, **windows, **kwargs)

    version = run([str(pg / ("postgres" + suffix)), "--version"], capture_output=True, text=True)
    if "(PostgreSQL) 17." not in version.stdout:
        raise ValueError("This verification requires PostgreSQL 17")
    print("Preparing a disposable PostgreSQL TLS server", flush=True)
    with tempfile.TemporaryDirectory(prefix="external-tls-", dir=runtime) as temporary:
        directory = Path(temporary).resolve()
        assert directory.is_relative_to(runtime.resolve())
        data = directory / "postgres"
        ca, key, cert = (directory / name for name in ("ca.crt", "server.key", "server.crt"))
        admin_password, app_password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        password_file = directory / "admin-password"
        password_file.write_text(admin_password, encoding="utf-8")
        quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE}
        run(
            [
                openssl,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "2",
                "-subj",
                "/CN=jev-test-ca",
                "-keyout",
                str(directory / "ca.key"),
                "-out",
                str(ca),
            ],
            **quiet,
        )
        other_ca = directory / "untrusted-ca.crt"
        run(
            [
                openssl,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "2",
                "-subj",
                "/CN=untrusted-test-ca",
                "-keyout",
                str(directory / "untrusted-ca.key"),
                "-out",
                str(other_ca),
            ],
            **quiet,
        )
        run(
            [
                openssl,
                "req",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-subj",
                "/CN=localhost",
                "-keyout",
                str(key),
                "-out",
                str(directory / "server.csr"),
            ],
            **quiet,
        )
        extensions = directory / "cert.ext"
        extensions.write_text(
            "subjectAltName=DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n"
        )
        run(
            [
                openssl,
                "x509",
                "-req",
                "-in",
                str(directory / "server.csr"),
                "-CA",
                str(ca),
                "-CAkey",
                str(directory / "ca.key"),
                "-CAcreateserial",
                "-days",
                "2",
                "-extfile",
                str(extensions),
                "-out",
                str(cert),
            ],
            **quiet,
        )
        if os.name != "nt":
            key.chmod(0o600)
        run(
            [
                str(pg / ("initdb" + suffix)),
                "-D",
                str(data),
                "-U",
                "external_admin",
                "-A",
                "scram-sha-256",
                "--encoding=UTF8",
                "--locale=C",
                "--pwfile",
                str(password_file),
            ],
            **quiet,
        )
        database_port = port()
        with (data / "postgresql.conf").open("a", encoding="utf-8") as output:
            output.write(
                f"\nlisten_addresses='127.0.0.1'\nport={database_port}\nssl=on\n"
                f"ssl_cert_file='{cert.as_posix()}'\nssl_key_file='{key.as_posix()}'\n"
            )
        control = [str(pg / ("pg_ctl" + suffix)), "-D", str(data), "-w"]
        app = None
        daemon_output = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        run([*control, "-l", str(directory / "postgres.log"), "start"], **daemon_output)
        try:
            url = URL.create(
                "postgresql+psycopg",
                username="external_admin",
                password=admin_password,
                host="localhost",
                port=database_port,
                database="postgres",
                query={
                    "sslmode": "verify-full",
                    "sslrootcert": str(ca),
                    "hostaddr": "127.0.0.1",
                    "connect_timeout": "3",
                },
            )
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("SDD_", "DATABASE_URL", "TYPESAFE_", "OPENAI_"))
            }
            environment.update(
                {
                    "SDD_TEST_ADMIN_URL": url.render_as_string(hide_password=False),
                    "SDD_TEST_TLS_ROOTCERT": str(ca),
                    "SDD_TEST_TLS_UNTRUSTED_CA": str(other_ca),
                }
            )
            migrate(url, "external_runtime", app_password)
            if full_suite:
                environment["SDD_TEST_POSTGRES_URL"] = url.set(
                    username="external_runtime", password=app_password
                ).render_as_string(hide_password=False)
            tests = (
                ["tests"]
                if full_suite
                else [
                    "tests/test_database_connections_postgres.py",
                    "tests/test_deployment_config.py",
                ]
            )
            print("Testing TLS identity and bounded connection pools", flush=True)
            checks = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    *tests,
                    "-q",
                    "--tb=short",
                    "-o",
                    "faulthandler_timeout=30",
                    "--basetemp=" + str(directory / "pytest"),
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                **windows,
            )
            print(checks.stdout, end="", flush=True)
            if checks.stderr:
                print(checks.stderr, end="", file=sys.stderr, flush=True)
            checks.check_returncode()
            print("Testing an authenticated workspace with structured TLS settings", flush=True)
            token = secrets.token_urlsafe(32)
            app_secret = directory / "app-password"
            app_secret.write_text(app_password, encoding="utf-8")
            tokens = directory / "tokens.json"
            tokens.write_text(
                json.dumps(
                    {token: {"tenant": "tls-workspace", "name": "reviewer", "role": "reviewer"}}
                )
            )
            environment.update(
                {
                    "SDD_ENV": "production",
                    "SDD_DB_HOST": "127.0.0.1",
                    "SDD_DB_PORT": str(database_port),
                    "SDD_DB_NAME": "postgres",
                    "SDD_DB_USER": "external_runtime",
                    "SDD_DB_PASSWORD_FILE": str(app_secret),
                    "SDD_DB_SSLMODE": "verify-full",
                    "SDD_DB_SSLROOTCERT": str(ca),
                    "SDD_DB_CONNECT_TIMEOUT": "3",
                    "SDD_API_TOKENS_FILE": str(tokens),
                    "SDD_FEATURE_MAINTENANCE": "0",
                }
            )
            environment = {
                key: value for key, value in environment.items() if not key.startswith("SDD_TEST_")
            }
            http_port = port()
            with (directory / "app.log").open("w", encoding="utf-8") as log:
                app = subprocess.Popen(
                    [sys.executable, "-m", "sdd.cli", "serve", "--port", str(http_port)],
                    cwd=directory,
                    env=environment,
                    stdout=log,
                    stderr=log,
                    **windows,
                )
                endpoint = f"http://127.0.0.1:{http_port}"
                deadline = time.monotonic() + 20
                while True:
                    try:
                        with urlopen(endpoint + "/ready", timeout=1) as response:
                            assert json.load(response)["status"] == "ready"
                        break
                    except OSError:
                        if app.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError(
                                "External application did not become ready"
                            ) from None
                        time.sleep(0.1)
                payload = {
                    "name": "notes",
                    "rows": [{"id": 1, "text": "请继续跟进"}],
                    "primary_key": ["id"],
                }
                request = Request(
                    endpoint + "/datasets",
                    data=json.dumps(payload).encode(),
                    headers={
                        "Authorization": "Bearer " + token,
                        "Content-Type": "application/json",
                    },
                )
                with urlopen(request, timeout=10) as response:
                    assert json.load(response)["name"] == "notes"
                with urlopen(
                    Request(endpoint + "/datasets", headers={"Authorization": "Bearer " + token}),
                    timeout=10,
                ) as response:
                    assert json.load(response)["datasets"][0]["name"] == "notes"
                print("Testing readiness and pool recovery after a PostgreSQL restart", flush=True)
                run([*control, "-m", "fast", "stop"], **daemon_output)
                try:
                    with urlopen(endpoint + "/ready", timeout=15):
                        raise AssertionError("Readiness accepted an unavailable database")
                except HTTPError as error:
                    assert error.code == 503
                with urlopen(endpoint + "/health", timeout=5) as response:
                    assert json.load(response)["status"] == "ok"
                run([*control, "-l", str(directory / "postgres.log"), "start"], **daemon_output)
                with urlopen(endpoint + "/ready", timeout=10) as response:
                    assert json.load(response)["status"] == "ready"
                with urlopen(
                    Request(endpoint + "/datasets", headers={"Authorization": "Bearer " + token}),
                    timeout=10,
                ) as response:
                    assert json.load(response)["datasets"][0]["name"] == "notes"
            print("External PostgreSQL TLS, bounded pools and authenticated HTTP workspace passed")
        finally:
            if app is not None and app.poll() is None:
                app.terminate()
                try:
                    app.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    app.kill()
                    app.wait(timeout=5)
            run([*control, "-m", "fast", "stop"], **daemon_output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-bin", required=True)
    parser.add_argument("--openssl", default="openssl")
    parser.add_argument(
        "--full-suite", action="store_true", help="Run all Python regressions over TLS"
    )
    args = parser.parse_args()
    verify(args.postgres_bin, args.openssl, args.full_suite)
