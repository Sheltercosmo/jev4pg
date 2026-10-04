"""Verify the native container, durable reuse and restored probability features."""

import json
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

from sqlalchemy import URL, create_engine, text
from sqlalchemy.exc import DBAPIError

from smoke import COMPOSE, command


BASIS = {
    "context_columns": ["body"],
    "questions": {
        "action": {"type": "noul", "instructions": "true", "subject_column": "body"},
        "unclear": {"type": "noul", "instructions": "unknown", "subject_column": "body"},
    },
}
EMBED = text(
    "SELECT * FROM jev_native.embed('SELECT id,body FROM container_source.notes ORDER BY id', "
    "CAST(:basis AS jsonb), CAST(:options AS jsonb))"
)


def main():
    assert os.getenv("SDD_TEST_NATIVE") == "1"
    directory = Path(os.getenv("SDD_SECRETS_DIR", ".secrets"))
    token = next(iter(json.loads((directory / "api_tokens.json").read_text())))
    url = URL.create(
        "postgresql+psycopg",
        username="sdd_admin",
        password=(directory / "postgres_password").read_text().strip(),
        host="127.0.0.1",
        port=int(os.getenv("SDD_POSTGRES_PORT", "5432")),
        database="sdd",
    )
    app_url = url.set(username="sdd_app", password=(directory / "app_password").read_text().strip())
    admin = create_engine(url, hide_parameters=True)
    app = create_engine(app_url, hide_parameters=True)
    basis = json.dumps(BASIS)
    options = {"max_requests": 10, "max_judgments": 20, "evidence_scope": "container-check"}

    with admin.begin() as connection:
        connection.exec_driver_sql("CREATE SCHEMA container_source AUTHORIZATION sdd_app")
    with app.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE container_source.notes(id integer PRIMARY KEY, body text)"
        )
        connection.execute(
            text(
                "INSERT INTO container_source.notes VALUES "
                "(1,'Please follow up.'),(2,'请继续跟进。'),(3,'Please follow up.'),(4,NULL)"
            )
        )
    with app.begin() as connection:
        rows = (
            connection.execute(EMBED, {"basis": basis, "options": json.dumps(options)})
            .mappings()
            .all()
        )
        assert len(rows) == 4
        assert max(row["usage"]["requests"] for row in rows) == 2
        assert rows[0]["embedding"]["complete"] is True
        assert rows[0]["decisions"]["unclear"]["output_state"] == "UNKNOWN"
        assert rows[3]["embedding"]["complete"] is False
        assert rows[3]["decisions"]["action"]["operation_state"] == "SKIPPED"
        assert rows[0]["embedding"] == rows[2]["embedding"]
        assert rows[0]["receipt"]["storage_state"] == "STORED"
        connection.exec_driver_sql(
            "CREATE TABLE sdd_data.container_embeddings(id integer, embedding jsonb)"
        )
        for row in rows:
            connection.execute(
                text(
                    "INSERT INTO sdd_data.container_embeddings VALUES (:id,CAST(:embedding AS jsonb))"
                ),
                {"id": row["source"]["id"], "embedding": json.dumps(row["embedding"])},
            )

    command(
        "run",
        "--rm",
        "migrate",
        "attach",
        "container_notes",
        "--tenant",
        "demo",
        "--schema",
        "container_source",
        "--table",
        "notes",
    )
    queries = [
        "SELECT id, SEMANTIC(body,'true') AS needs_action FROM container_notes WHERE body IS NOT NULL ORDER BY id",
        "WITH q AS (SELECT id,body FROM container_notes WHERE body IS NOT NULL) "
        "SELECT id,SEMANTIC(body,'true') AS needs_action FROM q ORDER BY id",
        "SELECT id,CASE WHEN id=1 THEN SEMANTIC(body,'true') ELSE false END AS needs_action "
        "FROM container_notes WHERE body IS NOT NULL ORDER BY id",
    ]
    for index, query in enumerate(queries):
        request = Request(
            "http://127.0.0.1:" + os.getenv("SDD_HTTP_PORT", "8000") + "/data/sql",
            data=json.dumps({"sql": query}).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
        assert result["manifest"]["execution_backend"] == "rust_postgresql", result
        assert [row["id"] for row in result["result"]] == [1, 2, 3], result
        expected = [True, True, True] if index < 2 else [True, False, False]
        assert [row["needs_action"] for row in result["result"]] == expected, result

    with admin.connect() as connection:
        assert (
            connection.execute(text("SELECT count(*) FROM jev_native.evidence")).scalar_one() >= 2
        )
        assert (
            connection.execute(
                text("SELECT rolconnlimit FROM pg_roles WHERE rolname='jev_registry'")
            ).scalar_one()
            == 8
        )
        assert not connection.execute(
            text("SELECT has_table_privilege('jev_registry','container_source.notes','SELECT')")
        ).scalar_one()
    with app.connect() as connection:
        try:
            connection.exec_driver_sql("SELECT * FROM jev_native.evidence")
        except DBAPIError:
            connection.rollback()
        else:
            raise AssertionError("The application can read private registry tables")

    app.dispose()
    admin.dispose()
    command("restart", "postgres", "app", "sql-worker")
    command("up", "-d", "--wait")
    with app.connect() as connection:
        replay = (
            connection.execute(
                EMBED, {"basis": basis, "options": json.dumps({**options, "max_requests": 0})}
            )
            .mappings()
            .all()
        )
        assert len(replay) == 4
        assert max(row["usage"]["requests"] for row in replay) == 0
        assert replay[0]["embedding"] == rows[0]["embedding"]
        assert replay[0]["receipt"]["storage_state"] == "REUSED"
    command("run", "--rm", "migrate")

    for grant, revoke in (
        ("GRANT pg_read_all_data TO jev_registry", "REVOKE pg_read_all_data FROM jev_registry"),
        (
            "GRANT SELECT ON container_source.notes TO jev_registry",
            "REVOKE SELECT ON container_source.notes FROM jev_registry",
        ),
    ):
        with admin.begin() as connection:
            connection.exec_driver_sql(grant)
        try:
            command("run", "--rm", "migrate")
        except subprocess.CalledProcessError:
            pass
        else:
            raise AssertionError("Migration accepted an overprivileged registry login")
        finally:
            with admin.begin() as connection:
                connection.exec_driver_sql(revoke)
    command("run", "--rm", "migrate")

    claim = text(
        "SELECT jev_native._registry_claim('restore-check',:identity,:pool,1,100,1000,86400,false)"
    )
    with admin.begin() as connection:
        attempt = connection.execute(claim, {"identity": "a" * 64, "pool": "b" * 64}).scalar_one()
        assert attempt["state"] == "CLAIMED"
        connection.execute(
            text(
                "UPDATE jev_native.request_attempts SET state='UNCERTAIN' WHERE id=CAST(:id AS uuid)"
            ),
            {"id": attempt["attempt"]},
        )
        accounting = connection.execute(
            text(
                "SELECT (SELECT count(*) FROM jev_native.evidence), "
                "(SELECT count(*) FROM jev_native.request_attempts), "
                "(SELECT sum(requests) FROM jev_native.request_allowances), "
                "(SELECT sum(active) FROM jev_native.request_pools)"
            )
        ).one()

    with tempfile.TemporaryDirectory() as temporary:
        archive = Path(temporary) / "native.dump"
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
        command("exec", "-T", "postgres", "createdb", "-U", "sdd_admin", "native_restore")
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
                    "native_restore",
                    "--exit-on-error",
                ],
                stdin=source,
                check=True,
            )
    restored = create_engine(url.set(database="native_restore"), hide_parameters=True)
    with restored.begin() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM sdd_data.container_embeddings")
            ).scalar_one()
            == 4
        )
        assert (
            connection.execute(text("SELECT count(*) FROM jev_native.evidence")).scalar_one() >= 2
        )
        distance = connection.execute(
            text(
                "SELECT jev_native.embedding_distance(a.embedding,b.embedding) "
                "FROM sdd_data.container_embeddings a, sdd_data.container_embeddings b WHERE a.id=1 AND b.id=3"
            )
        ).scalar_one()
        assert distance == 0
        assert (
            connection.execute(
                text(
                    "SELECT (SELECT count(*) FROM jev_native.evidence), "
                    "(SELECT count(*) FROM jev_native.request_attempts), "
                    "(SELECT sum(requests) FROM jev_native.request_allowances), "
                    "(SELECT sum(active) FROM jev_native.request_pools)"
                )
            ).one()
            == accounting
        )
        for table in ("evidence", "request_attempts"):
            next_value, maximum = connection.exec_driver_sql(
                f"SELECT nextval('jev_native.{table}_sequence_seq'), max(sequence) FROM jev_native.{table}"
            ).one()
            assert next_value > maximum
        blocked = connection.execute(claim, {"identity": "c" * 64, "pool": "b" * 64}).scalar_one()
        assert blocked["state"] == "SATURATED"
        assert connection.execute(
            text(
                "SELECT jev_native.reconcile_attempt(CAST(:id AS uuid),'CLOSED','Restore fixture verified; no external dispatch occurred')"
            ),
            {"id": attempt["attempt"]},
        ).scalar_one()
        assert (
            connection.execute(claim, {"identity": "c" * 64, "pool": "b" * 64}).scalar_one()[
                "state"
            ]
            == "CLAIMED"
        )
    restored.dispose()
    app.dispose()
    admin.dispose()
    print(
        "Native container passed: SQL and HTTP execution, Chinese text, duplicate and NULL states, "
        "restricted registry, zero-call reuse after restart, repeat migration, embedding restore, "
        "sequence continuity and preserved uncertain admission."
    )


if __name__ == "__main__":
    main()
