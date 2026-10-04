"""Exercise a fresh Compose stack, SQL clients, restart persistence and a backup restore."""

import json
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from sqlalchemy import URL, create_engine, text
from sdd.bootstrap import ensure_login, grant_client

COMPOSE = ["docker", "compose", "-f", "compose.yaml", "-f", "deploy/compose.test.yaml"]
if os.getenv("SDD_TEST_NATIVE") == "1":
    COMPOSE[4:4] = ["-f", "compose.native.yaml"]


def command(*args, input=None, env=None):
    result = subprocess.run(
        [*COMPOSE, *args], input=input, capture_output=True, text=True, encoding="utf-8", env=env
    )
    if result.returncode:
        print(result.stderr[-4000:])
        result.check_returncode()
    return result.stdout.strip()


def main():
    directory = Path(os.getenv("SDD_SECRETS_DIR", ".secrets"))
    token = next(iter(json.loads((directory / "api_tokens.json").read_text())))
    api = "http://127.0.0.1:" + os.getenv("SDD_HTTP_PORT", "8000")

    def request(path, body=None, authenticated=True):
        headers = {"Authorization": "Bearer " + token} if authenticated else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        with urlopen(
            Request(
                api + path,
                headers=headers,
                data=json.dumps(body).encode() if body is not None else None,
            ),
            timeout=15,
        ) as response:
            return json.load(response)

    assert request("/ready", authenticated=False)["status"] == "ready"
    try:
        request("/datasets", authenticated=False)
        raise AssertionError("Unauthenticated catalog access succeeded")
    except HTTPError as exc:
        assert exc.code in (401, 403)
    dataset = request(
        "/datasets",
        {
            "name": "deployment_notes",
            "primary_key": ["id"],
            "rows": [{"id": 1, "text": "问题仍未解决。"}, {"id": 2, "text": "Please follow up."}],
        },
    )
    admin_url = URL.create(
        "postgresql+psycopg",
        username="sdd_admin",
        password=(directory / "postgres_password").read_text().strip(),
        host="127.0.0.1",
        port=int(os.getenv("SDD_POSTGRES_PORT", "5432")),
        database="sdd",
    )
    engine = create_engine(admin_url, hide_parameters=True)
    password = secrets.token_urlsafe(32)
    with engine.begin() as connection:
        ensure_login(connection, "smoke_client", password)
    grant_client(admin_url, "smoke_client", "demo", "smoke-client")
    sql_env = {**os.environ, "PGPASSWORD": password, "PGCLIENTENCODING": "UTF8"}

    def sql(statement):
        return command(
            "exec",
            "-T",
            "-e",
            "PGPASSWORD",
            "-e",
            "PGCLIENTENCODING",
            "postgres",
            "psql",
            "-h",
            "127.0.0.1",
            "-U",
            "smoke_client",
            "-d",
            "sdd",
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            input=statement,
            env=sql_env,
        )

    jobs = []
    command("stop", "sql-worker")
    for proposition in ("true", "false", "unknown"):
        args = json.dumps(
            {"state": "问题仍未解决。", "proposition": proposition}, ensure_ascii=False
        )
        jobs.append(sql("SELECT jev.submit('NOUL', '" + args + "');"))
    assert json.loads(sql(f"SELECT jev.result('{jobs[0]}');"))["output_state"] == "NOT_EVALUATED"
    command("up", "-d", "--wait", "sql-worker")
    deadline = time.monotonic() + 90
    while True:
        results = [json.loads(sql(f"SELECT jev.result('{job}');")) for job in jobs]
        if all(r["job_state"] not in {"QUEUED", "RUNNING"} for r in results):
            break
        assert time.monotonic() < deadline, "SQL jobs did not complete"
        time.sleep(0.5)
    observations = [next(iter(r["observations"].values())) for r in results]
    assert observations[0]["value"] is True
    assert observations[1]["value"] is False
    assert observations[2]["output_state"] == "UNKNOWN"
    batch = request(
        "/jev/call",
        {
            "operator": "TAG",
            "arguments": {
                "subjects": {"dataset_id": dataset["id"]},
                "concept_revs": ["Needs follow-up", "Describes a problem"],
            },
        },
    )
    assert batch["output_state"] == "VALUE" and len(batch["observations"]) == 4
    command("run", "--rm", "migrate")
    command("restart", "postgres", "app", "sql-worker")
    command("up", "-d", "--wait")
    assert json.loads(sql(f"SELECT jev.result('{jobs[0]}');"))["job_state"] == "COMPLETED"
    assert request("/datasets")["datasets"][0]["id"] == dataset["id"]

    # A fresh restore exercises extension config tables, role grants and ordinary source data.
    with tempfile.TemporaryDirectory() as temporary:
        archive = Path(temporary) / "sdd.dump"
        with archive.open("wb") as output:
            subprocess.run(
                [
                    *COMPOSE,
                    "exec",
                    "-T",
                    "postgres",
                    "pg_dump",
                    "-U",
                    "sdd_admin",
                    "-d",
                    "sdd",
                    "-Fc",
                ],
                stdout=output,
                check=True,
            )
        command("exec", "-T", "postgres", "createdb", "-U", "sdd_admin", "sdd_restore")
        with archive.open("rb") as source:
            subprocess.run(
                [
                    *COMPOSE,
                    "exec",
                    "-T",
                    "postgres",
                    "pg_restore",
                    "-U",
                    "sdd_admin",
                    "-d",
                    "sdd_restore",
                    "--exit-on-error",
                ],
                stdin=source,
                check=True,
            )
        restored = create_engine(admin_url.set(database="sdd_restore"), hide_parameters=True)
        with restored.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT count(*) FROM jev.jobs WHERE state='COMPLETED'")
                ).scalar_one()
                == 3
            )
            assert (
                connection.execute(text("SELECT count(*) FROM dataset_catalog")).scalar_one() == 1
            )
        restored.dispose()
    engine.dispose()
    print(
        "Compose smoke passed: API, psql, batching, states, repeat migration, restart and backup restore."
    )


if __name__ == "__main__":
    main()
