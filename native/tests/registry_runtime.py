"""Exercise durable native evidence across actual PostgreSQL backends and HTTP calls."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from sqlalchemy import URL

from sdd.db import Database
from sdd.generic.sql import SQLService


QUESTIONS = {"q": {"type": "noul", "instructions": "Complete? 完成了吗？"}}


def scan(connection, source, scope, *, questions=None, **options):
    return connection.execute(
        "SELECT source,decisions,observation,usage,receipt FROM jev_native.scan(%s,%s,%s)",
        (source, Jsonb(questions or QUESTIONS), Jsonb({"evidence_scope": scope, **options})),
    ).fetchall()


def wait_for_calls(observations, expected, future):
    deadline = time.monotonic() + 5
    while len(observations) < expected and not future.done() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(observations) == expected


def verify_registry_runtime(connection, observations, gates, config_path):
    original = config_path.read_text()
    config = json.loads(original)
    registry_dsn = make_conninfo(
        host=connection.info.host,
        port=connection.info.port,
        dbname=connection.info.dbname,
        user="native_coordinator",
    )
    config["registry"] = {"dsn": registry_dsn, "max_active": 4, "max_daily": 1000}
    config_path.write_text(json.dumps(config))
    checks = []

    def run(source, scope, **options):
        with psycopg.connect(connection.info.dsn, autocommit=True) as client:
            return scan(client, source, scope, **options)

    try:
        from policy import verify_registry_policy

        checks.extend(verify_registry_policy(connection, observations))
        source = "SELECT 1 AS id,'完成' AS note,0.7 AS p"
        start = len(observations)
        connection.execute("BEGIN")
        first = scan(connection, source, "replay")[0]
        connection.execute("ROLLBACK")
        assert first[1]["q"]["output_state"] == "UNKNOWN"
        assert first[4]["storage_state"] == "STORED"
        second = run(source, "replay", accept=0.6, max_requests=0)[0]
        assert second[1]["q"]["value"] is True
        assert second[2] == first[2]
        assert second[3]["requests"] == 0 and second[3]["durable_reused_rows"] == 1
        assert second[4] == {"attempt_id": first[4]["attempt_id"], "storage_state": "REUSED"}
        assert len(observations) == start + 1
        checks.append(
            "native evidence survives source rollback and replays a new policy with zero requests"
        )

        start = len(observations)
        for modified in (
            "SELECT 1 AS id,'pending' AS note,0.7 AS p",
            "SELECT 1 AS 编号,'完成' AS 说明,0.7 AS p",
        ):
            assert run(modified, "replay")[0][4]["storage_state"] == "STORED"
        paraphrase = {"q": {"type": "noul", "instructions": "Has the work finished?"}}
        assert run(source, "replay", questions=paraphrase)[0][4]["storage_state"] == "STORED"
        config["revision"] = "transport-test-v2"
        config_path.write_text(json.dumps(config))
        assert run(source, "replay")[0][4]["storage_state"] == "STORED"
        config["revision"] = "transport-test-v1"
        config_path.write_text(json.dumps(config))
        assert run(source, "replay", evidence_max_age_seconds=0)[0][4]["storage_state"] == "STORED"
        assert len(observations) == start + 5
        checks.append(
            "changed values, schema names, definitions, evaluator revisions and freshness bounds prevent stale reuse"
        )

        gates["registry-parallel"] = threading.Event()
        start = len(observations)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                run,
                "SELECT id,'registry-parallel' AS gate FROM generate_series(1,4) AS id",
                "parallel-admission",
            )
            try:
                wait_for_calls(observations, start + 4, future)
            finally:
                gates["registry-parallel"].set()
            parallel = future.result(timeout=5)
        assert len(parallel) == 4
        assert all(row[4]["storage_state"] == "STORED" for row in parallel)
        assert parallel[-1][3]["requests"] == 4
        checks.append("durable admission leaves independent provider requests concurrent")

        gated = "SELECT 'same-context' AS note,'registry-single' AS gate"
        gates["registry-single"] = threading.Event()
        start = len(observations)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run, gated, "concurrent")
            try:
                wait_for_calls(observations, start + 1, future)
                other = run(gated, "concurrent")[0]
                assert other[1]["q"]["operation_state"] == "BLOCKED_BY_CONCURRENCY"
                assert other[1]["q"]["output_state"] == "NOT_EVALUATED"
                assert other[3]["requests"] == 0
                mixed = run(
                    gated + " UNION ALL SELECT 'independent',NULL::text",
                    "concurrent",
                    max_requests=1,
                    max_judgments=1,
                )
                assert mixed[0][1]["q"]["operation_state"] == "BLOCKED_BY_CONCURRENCY"
                assert mixed[1][4]["storage_state"] == "STORED"
                assert mixed[1][3]["requests"] == mixed[1][3]["judgments"] == 1
            finally:
                gates["registry-single"].set()
            completed = future.result(timeout=5)[0]
        assert other[4]["attempt_id"] == completed[4]["attempt_id"]
        assert run(gated, "concurrent")[0][4]["storage_state"] == "REUSED"
        assert len(observations) == start + 2
        checks.append(
            "overlapping native scans share durable ownership without duplicating provider work"
        )
        checks.append("declined ownership does not consume another row's local query allowance")

        config["registry"]["max_active"] = 1
        config_path.write_text(json.dumps(config))
        gates["registry-cap"] = threading.Event()
        start = len(observations)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run, "SELECT 'first' AS note,'registry-cap' AS gate", "global-a")
            try:
                wait_for_calls(observations, start + 1, future)
                held = run("SELECT 'second' AS note", "global-b")[0]
                assert held[1]["q"]["operation_state"] == "COORDINATION_SATURATED"
                assert held[3]["requests"] == 0 and len(observations) == start + 1
            finally:
                gates["registry-cap"].set()
            future.result(timeout=5)
        assert run("SELECT 'second' AS note", "global-b")[0][4]["storage_state"] == "STORED"
        checks.append("a provider-wide request limit spans independent query backends and scopes")
        sequential = run("SELECT generate_series(1,3) AS id", "local-capacity")
        assert all(row[4]["storage_state"] == "STORED" for row in sequential)
        assert sequential[-1][3]["requests"] == 3
        checks.append("one scan respects provider capacity without rejecting its own queued rows")

        gates["registry-cancel"] = threading.Event()
        cancelled, keep_open = threading.Event(), threading.Event()
        backend = []
        cancelled_source = "SELECT 'cancel-me' AS note,'registry-cancel' AS gate"

        def cancel_worker():
            with psycopg.connect(connection.info.dsn, autocommit=True) as client:
                backend.append(client.info.backend_pid)
                try:
                    scan(client, cancelled_source, "cancel")
                except psycopg.errors.QueryCanceled:
                    assert client.execute("SELECT 42").fetchone()[0] == 42
                    cancelled.set()
                    keep_open.wait(5)
                else:
                    raise AssertionError("Expected statement cancellation")

        start = len(observations)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(cancel_worker)
            try:
                wait_for_calls(observations, start + 1, future)
                assert connection.execute("SELECT pg_cancel_backend(%s)", (backend[0],)).fetchone()[
                    0
                ]
                assert cancelled.wait(3)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    active = connection.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE application_name='jev-native-registry'"
                    ).fetchone()[0]
                    if not active:
                        break
                    time.sleep(0.02)
                assert active == 0, "Cancelled scans leaked a control connection"
                connection.execute(
                    "UPDATE jev_native.request_attempts SET deadline=clock_timestamp()-interval '1 second' "
                    "WHERE CASE WHEN scope IS JSON OBJECT THEN scope::jsonb->>'scope' END='cancel'"
                )
                held = run(cancelled_source, "cancel")[0]
                assert held[1]["q"]["operation_state"] == "UNCERTAIN"
                assert held[3]["requests"] == 0 and len(observations) == start + 1
            finally:
                gates["registry-cancel"].set()
                keep_open.set()
            future.result(timeout=5)
        connection.execute(
            "SELECT jev_native.reconcile_attempt(%s,'RETRY_ALLOWED','Fixture request has ended')",
            (held[4]["attempt_id"],),
        )
        assert run(cancelled_source, "cancel")[0][4]["storage_state"] == "STORED"
        assert len(observations) == start + 2
        checks.append(
            "cancellation closes the control connection and retains an uncertain dispatch until reconciliation"
        )

        gates["registry-publish"] = threading.Event()
        start = len(observations)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                run, "SELECT 'known-value' AS note,'registry-publish' AS gate", "publish-failure"
            )
            try:
                wait_for_calls(observations, start + 1, future)
                connection.execute(
                    "REVOKE EXECUTE ON FUNCTION jev_native._registry_finish(uuid,jsonb,text) FROM native_coordinator"
                )
                gates["registry-publish"].set()
                result = future.result(timeout=5)[0]
                assert result[1]["q"]["value"] is True and result[2] is not None
                assert result[4]["storage_state"] == "UNCONFIRMED"
            finally:
                connection.execute(
                    "GRANT EXECUTE ON FUNCTION jev_native._registry_finish(uuid,jsonb,text) TO native_coordinator"
                )
                gates["registry-publish"].set()
        connection.execute(
            "SELECT jev_native.reconcile_attempt(%s,'CLOSED','Known fixture response; publication fault tested')",
            (result[4]["attempt_id"],),
        )
        checks.append(
            "publication failure preserves a known semantic value and exposes its unconfirmed receipt"
        )

        connection.execute("ALTER ROLE native_coordinator CONNECTION LIMIT 1")
        start = len(observations)
        try:
            with psycopg.connect(registry_dsn, autocommit=True):
                held = run("SELECT 'connection-cap' AS note", "capacity")[0]
                assert held[1]["q"]["operation_state"] == "COORDINATION_UNAVAILABLE"
                assert held[3]["requests"] == 0 and len(observations) == start
        finally:
            connection.execute("ALTER ROLE native_coordinator CONNECTION LIMIT -1")
        checks.append("control-connection exhaustion prevents uncoordinated provider dispatch")

        role_source = "SELECT 'role-scope' AS note"
        start = len(observations)
        run(role_source, "same-name")
        connection.execute("SET ROLE native_reader")
        try:
            assert scan(connection, role_source, "same-name")[0][4]["storage_state"] == "STORED"
        finally:
            connection.execute("RESET ROLE")
        assert len(observations) == start + 2
        checks.append(
            "role identity derived by PostgreSQL partitions evidence even when caller scope text matches"
        )

        config["registry"]["max_active"] = 4
        config_path.write_text(json.dumps(config))
        url = URL.create(
            "postgresql+psycopg",
            username="native_application",
            host=connection.info.host,
            port=connection.info.port,
            database=connection.info.dbname,
        )
        db = Database(url)
        try:
            service = SQLService(db, semantic_engine="native")
            query = "SELECT COUNT(*) AS n FROM work_items WHERE SEMANTIC(note,'Complete?')"
            first = service.execute("native-application-test", query)
            start = len(observations)
            reused = service.execute("native-application-test", query, max_evaluations=0)
            assert first["result"] == reused["result"] == [{"n": 2}]
            assert reused["manifest"]["semantic_coverage"]["requests"] == 0
            assert reused["manifest"]["semantic_coverage"]["durable_reused_rows"] == 3
            assert reused["manifest"]["evidence_retention"] == "native_registry"
            assert len(reused["manifest"]["evidence_receipts"]) == 3
            assert len(observations) == start
        finally:
            db.engine.dispose()
        checks.append(
            "application semantic reads reuse durable evidence under zero new-call allowance"
        )
        return checks
    finally:
        config_path.write_text(original)
