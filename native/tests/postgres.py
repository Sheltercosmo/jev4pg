"""Exercise compiled PostgreSQL interfaces with deterministic transport fixtures."""

import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from decimal import Decimal

import psycopg
import sqlglot
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
PG_BIN = Path(os.environ.get("PG_BIN", "/usr/lib/postgresql/17/bin"))
QUESTIONS = {"done": {"type": "noul", "instructions": "Has the work been completed? 完成了吗？"}}
observations = []
gates = {}
lock = threading.Lock()
active = peak = 0


class Model(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        global active, peak
        payload = json.loads(
            self.rfile.read(int(self.headers["Content-Length"])), parse_float=Decimal
        )
        with lock:
            observations.append(payload)
            active += 1
            peak = max(peak, active)
        try:
            if gate := payload["state"].get("gate"):
                gates[gate].wait(timeout=3)
            time.sleep(float(payload["state"].get("delay", 0.025)))
            probability = payload["state"].get("p", 0.95)
            if isinstance(probability, Decimal):
                probability = float(probability)
            answers = {name: {"type": "noul", "noul": probability} for name in payload["questions"]}
            answers.update(payload["state"].get("fixture_answers", {}))
            body = json.dumps({"model": "fixture-v1", "answers": answers}, default=float).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            with lock:
                active -= 1


def scan(connection, source, options=None, questions=None):
    return connection.execute(
        "SELECT ordinal, source, decisions FROM jev_native.scan(%s,%s,%s)",
        (source, Jsonb(questions or QUESTIONS), Jsonb(options or {})),
    ).fetchall()


def must_fail(operation, fragment=None):
    try:
        operation()
    except psycopg.Error as error:
        if fragment:
            assert fragment.lower() in str(error).lower(), str(error)
        return
    raise AssertionError("Expected PostgreSQL to reject the operation")


def verify(connection, config):
    global peak
    checks = []

    connection.execute("CREATE EXTENSION jev_native")
    connection.execute("CREATE TABLE work(id integer, note text, p numeric, private_note text)")
    connection.execute(
        "INSERT INTO work VALUES (1,'完成',0.95,'secret'),(2,'pending',0.05,'secret'),(3,NULL,0.5,'secret')"
    )
    rows = scan(connection, "SELECT id,note,p FROM work ORDER BY id")
    assert [r[0] for r in rows] == [1, 2, 3]
    assert rows[0][2]["done"]["value"] is True
    assert rows[1][2]["done"]["value"] is False
    assert rows[2][2]["done"]["output_state"] == "UNKNOWN"
    assert "private_note" not in observations[-1]["state"]
    assert any(item["state"]["note"] == "完成" for item in observations)
    checks.append("typed outcomes, Unicode, NULL and exact projection")

    start = len(observations)
    rows = scan(connection, "SELECT id,note,p FROM work WHERE id=1")
    assert len(rows) == 1 and len(observations) == start + 1
    checks.append("structured source filtering before provider dispatch")

    start = len(observations)
    rows = scan(
        connection,
        "SELECT note,p FROM work WHERE id=1 UNION ALL SELECT note,p FROM work WHERE id=1",
        {"batch_rows": 1},
    )
    assert len(rows) == 2 and len(observations) == start + 1
    checks.append("duplicate multiplicity with cross-batch context reuse")

    rows = scan(
        connection, "SELECT id,p FROM work ORDER BY id", {"batch_rows": 1, "max_requests": 1}
    )
    assert rows[0][2]["done"]["output_state"] == "VALUE"
    assert all(
        r[2]["done"]["output_state"] == "NOT_EVALUATED"
        and r[2]["done"]["operation_state"] == "BLOCKED_BY_BUDGET"
        for r in rows[1:]
    )
    must_fail(
        lambda: connection.execute(
            "SELECT jev_native.require_bool(%s,%s)", (Jsonb(rows[1][2]), "done")
        ),
        "unresolved",
    )
    must_fail(
        lambda: connection.execute(
            "SELECT count(*) FROM jev_native.scan('SELECT id,p FROM work',%s) WHERE jev_native.require_bool(decisions,'done')",
            (Jsonb(QUESTIONS),),
        ),
        "unresolved",
    )
    checks.append("one query budget and unresolved population protection")
    must_fail(
        lambda: connection.execute("SELECT jev_native.require_bool(NULL,'done')"), "unresolved"
    )
    must_fail(lambda: scan(connection, None), "required")
    strict = connection.execute(
        "SELECT proisstrict FROM pg_proc WHERE oid='jev_native.require_bool(jsonb,text)'::regprocedure"
    ).fetchone()[0]
    assert not strict
    checks.append("SQL NULL cannot bypass semantic membership validation")

    invalid = scan(connection, "SELECT 'invalid probability' AS p")
    assert invalid[0][2]["done"]["operation_state"] == "FAILED"
    assert invalid[0][2]["done"]["output_state"] == "NOT_EVALUATED"
    checks.append("malformed provider results remain operational failures")

    from policy import verify_policy

    checks.extend(verify_policy(connection, observations))

    from plans import verify_plans

    checks.extend(verify_plans(connection, observations, gates))

    from conditional_plans import verify_conditionals

    checks.extend(verify_conditionals(connection, observations, gates))

    connection.execute(
        "CREATE TABLE evidence_source AS SELECT 1 AS id,'完成'::text AS note,0.7::numeric AS p"
    )
    connection.execute(
        "CREATE TABLE saved_evidence AS SELECT * FROM jev_native.scan('SELECT * FROM evidence_source',%s)",
        (Jsonb(QUESTIONS),),
    )
    start = len(observations)
    with psycopg.connect(connection.info.dsn, autocommit=True) as replay:
        saved, revised, evidence = replay.execute(
            "SELECT decisions,jev_native.decide(source,observation,'{\"accept\":0.65}'),observation FROM saved_evidence"
        ).fetchone()
        assert saved["done"]["output_state"] == "UNKNOWN"
        assert revised["done"]["value"] is True
        assert evidence["evaluator"]["model"] == "fixture-v1"
        assert evidence["evaluator"]["revision"] == "transport-test-v1"
        assert evidence["response"]["answers"]["done"]["noul"] == 0.7
        assert "endpoint" not in evidence["evaluator"]
        connection.execute("UPDATE evidence_source SET note='pending',p=0.05")
        must_fail(
            lambda: replay.execute(
                "SELECT jev_native.decide(to_jsonb(live),saved.observation) FROM evidence_source live CROSS JOIN saved_evidence saved"
            ),
            "context does not match",
        )
        connection.execute("DELETE FROM evidence_source")
        assert replay.execute("SELECT count(*) FROM saved_evidence").fetchone()[0] == 1
        assert (
            replay.execute(
                "SELECT jev_native.decide(source,observation)->'done'->>'output_state' FROM saved_evidence"
            ).fetchone()[0]
            == "UNKNOWN"
        )
        must_fail(lambda: replay.execute("SELECT jev_native.decide('{}',NULL)"), "not evaluated")
        must_fail(
            lambda: replay.execute(
                'SELECT jev_native.decide(source,observation,\'{"accept":0.1,"reject":0.2}\') FROM saved_evidence'
            ),
            "thresholds",
        )
    assert len(observations) == start
    checks.append(
        "durable observations replay across sessions without requests and reject changed contexts"
    )

    for literal in ["1e-7::float8", "-0.0::numeric", "900719925474099312345.123456789::numeric"]:
        result = connection.execute(
            "SELECT jev_native.decide(source,observation)->'done'->>'value' FROM jev_native.scan(%s,%s)",
            (f"SELECT {literal} AS amount", Jsonb(QUESTIONS)),
        ).fetchone()[0]
        assert result == "true"
    no_evidence = connection.execute(
        "SELECT observation,decisions FROM jev_native.scan('SELECT 1 AS id',%s,'{\"max_requests\":0}')",
        (Jsonb(QUESTIONS),),
    ).fetchone()
    assert no_evidence[0] is None
    assert no_evidence[1]["done"]["output_state"] == "NOT_EVALUATED"
    checks.append(
        "evidence survives PostgreSQL numeric normalization and absent work has no observation"
    )

    large = "900719925474099312345.123456789"
    scan(connection, "SELECT " + large + "::numeric AS amount")
    # The provider sees the numeric JSON token without conversion through f64.
    assert observations[-1]["state"]["amount"] == Decimal(large)
    returned = connection.execute(
        "SELECT (source->>'amount')::numeric FROM jev_native.scan(%s,%s)",
        ("SELECT " + large + "::numeric AS amount", Jsonb(QUESTIONS)),
    ).fetchone()[0]
    assert returned == Decimal(large)
    checks.append("PostgreSQL numeric serialization path")

    peak = 0
    scan(connection, "SELECT id,0.1 AS delay FROM generate_series(1,6) id", {"concurrency": 3})
    assert peak == 3, peak
    checks.append("three concurrent independent requests")

    must_fail(lambda: scan(connection, "SELECT * FROM work", {"max_rows": 1}), "max_rows")
    must_fail(lambda: scan(connection, "SELECT 1); DELETE FROM work; SELECT (1"), None)
    assert connection.execute("SELECT count(*) FROM work").fetchone()[0] == 3
    checks.append("row limits and source statement rejection")
    start = len(observations)
    must_fail(lambda: scan(connection, "SELECT 1 AS x,2 AS x"), "unique column names")
    must_fail(lambda: scan(connection, "SELECT repeat('x',1000001) AS body"), "1 MB")
    assert len(observations) == start
    checks.append("duplicate names and oversized contexts rejected before dispatch")

    connection.execute("CREATE ROLE native_reader")
    connection.execute("GRANT USAGE ON SCHEMA jev_native TO native_reader")
    connection.execute("SET ROLE native_reader")
    must_fail(lambda: scan(connection, "SELECT 1"), "permission denied")
    connection.execute("RESET ROLE")
    connection.execute(
        "GRANT EXECUTE ON FUNCTION jev_native.scan(text,jsonb,jsonb) TO native_reader"
    )
    connection.execute("GRANT SELECT(id,note,p) ON work TO native_reader")
    connection.execute("SET ROLE native_reader")
    assert len(scan(connection, "SELECT id,p FROM work WHERE id=1")) == 1
    must_fail(lambda: scan(connection, "SELECT private_note FROM work"), "permission denied")
    connection.execute("RESET ROLE")
    connection.execute("ALTER TABLE work ENABLE ROW LEVEL SECURITY")
    connection.execute("CREATE POLICY one_row ON work FOR SELECT TO native_reader USING (id=1)")
    connection.execute("SET ROLE native_reader")
    assert len(scan(connection, "SELECT id,p FROM work")) == 1
    connection.execute("RESET ROLE")
    checks.append("explicit execution grant, column permissions and row security")

    connection.execute("SET statement_timeout='150ms'")
    started = time.monotonic()
    must_fail(lambda: scan(connection, "SELECT 2.0 AS delay"), "statement timeout")
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, elapsed
    connection.execute("RESET statement_timeout")
    assert connection.execute("SELECT 42").fetchone()[0] == 42
    checks.append("cancellation during provider I/O and backend recovery")

    with connection.transaction():
        before = connection.execute("SELECT count(*) FROM pg_cursors").fetchone()[0]
        connection.execute(
            "SELECT * FROM jev_native.scan('SELECT id FROM generate_series(1,3) id',%s) LIMIT 1",
            (Jsonb(QUESTIONS),),
        ).fetchall()
        after = connection.execute("SELECT count(*) FROM pg_cursors").fetchone()[0]
        assert after == before, (before, after)
    checks.append("portal cleanup after limited consumption")
    with connection.transaction():
        before = connection.execute("SELECT count(*) FROM pg_cursors").fetchone()[0]
        start = len(observations)
        connection.execute(
            "SELECT jev_native.scan('SELECT id FROM generate_series(1,100) id',%s,'{\"batch_rows\":1}') LIMIT 1",
            (Jsonb(QUESTIONS),),
        ).fetchall()
        assert len(observations) == start + 1
        assert connection.execute("SELECT count(*) FROM pg_cursors").fetchone()[0] == before
        connection.execute(
            "DECLARE result_cursor CURSOR FOR SELECT jev_native.scan('SELECT id FROM generate_series(1,100) id',%s,'{\"batch_rows\":1}')",
            (Jsonb(QUESTIONS),),
        )
        connection.execute("FETCH 1 FROM result_cursor").fetchall()
        connection.execute("CLOSE result_cursor")
        assert connection.execute("SELECT count(*) FROM pg_cursors").fetchone()[0] == before
    checks.append("ProjectSet early stop and explicit cursor close release source portals")
    with psycopg.connect(connection.info.dsn, autocommit=True) as bounded:
        scan(bounded, "SELECT 'warm' AS note")
        status = Path(f"/proc/{bounded.info.backend_pid}/status")

        def peak_kib():
            return int(
                next(
                    line.split()[1]
                    for line in status.read_text().splitlines()
                    if line.startswith("VmHWM:")
                )
            )

        before = peak_kib()
        start = len(observations)
        started = time.monotonic()
        result = bounded.execute(
            "SELECT count(*),sum(ordinal) FROM jev_native.scan(%s,%s,%s)",
            (
                "SELECT repeat('x',1000) AS note FROM generate_series(1,100000)",
                Jsonb(QUESTIONS),
                Jsonb({"max_rows": 100000, "max_requests": 1}),
            ),
        ).fetchone()
        assert result == (100000, 5000050000)
        assert len(observations) == start + 1
        memory_kib = peak_kib() - before
        assert memory_kib < 96 * 1024, memory_kib
        bulk_seconds = time.monotonic() - started
    checks.append(
        "100,000 duplicate contexts retain multiplicity with one request and bounded observed memory"
    )
    from application import verify_application
    from multi_source import verify_multi_source

    multi_checks, multi_metrics = verify_multi_source(connection, observations, gates, must_fail)
    checks.extend(multi_checks)
    checks.extend(verify_application(connection, observations))
    from embeddings import verify_embeddings

    checks.extend(verify_embeddings(connection, observations, gates, must_fail))
    from registry import verify_registry

    checks.extend(verify_registry(connection))
    from registry_runtime import verify_registry_runtime

    checks.extend(verify_registry_runtime(connection, observations, gates, config))
    if os.getenv("SDD_TEST_NATIVE_UPGRADE") == "1":
        from upgrade import verify_upgrade

        checks.extend(verify_upgrade(connection))
    if release_python := os.getenv("SDD_TEST_RELEASE_PYTHON"):
        from application_upgrade import verify_application_upgrade

        checks.extend(verify_application_upgrade(connection, release_python))
    from feature_publication import verify_feature_publication

    checks.extend(verify_feature_publication(connection))
    return {
        "checks": checks,
        "passed": len(checks),
        "provider": "deterministic fixture",
        "sqlglot_version": sqlglot.__version__,
        "nl_accuracy_measured": False,
        "cancellation_seconds": elapsed,
        "bulk_scan_seconds": bulk_seconds,
        "bulk_scan_incremental_peak_kib": memory_kib,
        "multi_source_fixture": multi_metrics,
    }


def main():
    model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="jev-native-") as directory:
            base = Path(directory)
            config = base / "provider.json"
            config.write_text(
                json.dumps(
                    {
                        "endpoint": f"http://127.0.0.1:{model.server_port}/v1/systemone",
                        "model": "fixture-v1",
                        "revision": "transport-test-v1",
                    }
                )
            )
            config.chmod(0o600)
            with socket.socket() as reserve:
                reserve.bind(("127.0.0.1", 0))
                port = reserve.getsockname()[1]
            environment = {**os.environ, "JEV_NATIVE_CONFIG_FILE": str(config)}
            subprocess.run(
                [
                    str(PG_BIN / "initdb"),
                    "-D",
                    str(base / "data"),
                    "-A",
                    "trust",
                    "--no-locale",
                    "-E",
                    "UTF8",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            subprocess.run(
                [
                    str(PG_BIN / "pg_ctl"),
                    "-D",
                    str(base / "data"),
                    "-l",
                    str(base / "postgres.log"),
                    "-o",
                    f"-h 127.0.0.1 -p {port} -k {directory}",
                    "-w",
                    "start",
                ],
                env=environment,
                check=True,
            )
            try:
                with psycopg.connect(
                    host="127.0.0.1", port=port, dbname="postgres", autocommit=True
                ) as connection:
                    result = verify(connection, config)
                (ROOT / "verification.json").write_text(json.dumps(result, indent=2) + "\n")
                print(json.dumps(result, indent=2))
            except BaseException:
                print((base / "postgres.log").read_text()[-10000:])
                raise
            finally:
                subprocess.run(
                    [str(PG_BIN / "pg_ctl"), "-D", str(base / "data"), "-m", "fast", "-w", "stop"],
                    check=True,
                )
    finally:
        model.shutdown()
        model.server_close()


if __name__ == "__main__":
    main()
