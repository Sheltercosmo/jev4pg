"""Verify the independent-source frontier against PostgreSQL and HTTP fixtures."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
from psycopg.types.json import Jsonb


QUESTION = {"q": {"type": "noul", "instructions": "Complete? 完成了吗？"}}


def source(identity, sql, questions=None):
    return {"id": identity, "sql": sql, "questions": questions or QUESTION}


def scan(connection, sources, options=None):
    return connection.execute(
        "SELECT source_id,ordinal,source,decisions,observation,usage FROM jev_native.scan_many(%s,%s)",
        (Jsonb(sources), Jsonb(options or {})),
    ).fetchall()


def open_portals(connection):
    # Prepared inspection queries have their own unnamed protocol portal.
    return connection.execute("SELECT name FROM pg_cursors WHERE name<>'' ORDER BY name").fetchall()


def verify_multi_source(connection, observations, gates, must_fail):
    checks = []
    plan = [
        source("left", "SELECT 1 AS id,'done' AS note,0.95 AS p,'frontier' AS gate"),
        source("分组'right", "SELECT 2 AS 编号,'pending' AS 说明,0.05 AS p,'frontier' AS gate"),
    ]
    gates["frontier"] = threading.Barrier(2)
    start = len(observations)
    rows = scan(connection, plan, {"concurrency": 2})
    assert [(row[0], row[1], row[3]["q"]["value"]) for row in rows] == [
        ("left", 1, True),
        ("分组'right", 1, False),
    ]
    assert len(observations) == start + 2
    assert all("source_id" not in item["state"] for item in observations[start:])
    assert max(row[5]["requests"] for row in rows) == 2
    checks.append(
        "different source populations overlap before either provider request can complete"
    )

    plan = [source("empty-first", "SELECT 1 AS id WHERE false")]
    for index in range(5):
        plan.append(
            source(
                str(index), f"SELECT id,{index} AS marker FROM generate_series(1,3) id ORDER BY id"
            )
        )
        if index in (1, 4):
            plan.append(source("empty-" + str(index), "SELECT 1 AS id WHERE false"))
    start = len(observations)
    rows = scan(connection, plan, {"batch_rows": 2, "max_requests": 5, "concurrency": 2})
    assert len(rows) == 15 and len(observations) == start + 5
    assert {row[0] for row in rows if row[3]["q"]["output_state"] == "VALUE"} == {
        str(i) for i in range(5)
    }
    assert all(row[3]["q"]["operation_state"] == "BLOCKED_BY_BUDGET" for row in rows if row[1] > 1)
    assert max(row[5]["requests"] for row in rows) == 5
    assert max(row[5]["judgments"] for row in rows) == 5
    assert all([row[1] for row in rows if row[0] == str(i)] == [1, 2, 3] for i in range(5))
    checks.append(
        "persistent round-robin admission spans batches, empty sources and exhausted budgets"
    )

    plan = [
        source(str(i), f"SELECT id,{i} AS marker FROM generate_series(1,5) id ORDER BY id")
        for i in range(3)
    ]
    rows = scan(connection, plan, {"batch_rows": 4, "max_requests": 8})
    admitted = [row[0] for row in rows if row[3]["q"]["output_state"] == "VALUE"]
    assert admitted == ["0", "1", "2", "0", "1", "2", "0", "1"]
    checks.append("uneven batch sizes rotate the extra admission across independent sources")

    same = [source("a", "SELECT 'done' AS note"), source("b", "SELECT 'done' AS note")]
    start = len(observations)
    rows = scan(connection, same)
    assert len(rows) == 2 and len(observations) == start + 1
    assert [row[0] for row in rows] == ["a", "b"]
    assert rows[0][4] == rows[1][4]
    same[1]["questions"] = {"q": {"type": "noul", "instructions": "Still pending?"}}
    start = len(observations)
    rows = scan(connection, same)
    assert len(observations) == start + 2
    assert rows[0][4]["questions"] != rows[1][4]["questions"]
    questions = {
        "missing": {"type": "noul", "instructions": "Complete?", "subject_column": "note"},
        "present": {"type": "noul", "instructions": "Complete?", "subject_column": "other"},
    }
    rows = scan(
        connection, [source("null", "SELECT NULL::text AS note,'完成' AS other", questions)]
    )
    assert rows[0][3]["missing"]["output_state"] == "NOT_EVALUATED"
    assert rows[0][3]["present"]["value"] is True
    checks.append(
        "cross-source reuse preserves multiplicity, question identity and skipped questions"
    )

    start = len(observations)
    for invalid in ([], [same[0], same[0]], [dict(same[0], unexpected=True)]):
        must_fail(lambda invalid=invalid: scan(connection, invalid))
    assert len(observations) == start
    must_fail(
        lambda: scan(
            connection,
            [source("a", "SELECT id FROM generate_series(1,2) id"), source("b", "SELECT 3 AS id")],
            {"max_rows": 2},
        ),
        "max_rows",
    )
    checks.append("source declarations and total row limits cannot multiply the query allowance")

    connection.execute("SET ROLE native_reader")
    must_fail(lambda: scan(connection, [source("a", "SELECT id FROM work")]), "permission denied")
    connection.execute("RESET ROLE")
    connection.execute(
        "GRANT EXECUTE ON FUNCTION jev_native.scan_many(jsonb,jsonb) TO native_reader"
    )
    connection.execute("SET ROLE native_reader")
    rows = scan(
        connection, [source("a", "SELECT id,p FROM work"), source("b", "SELECT id,p FROM work")]
    )
    assert len(rows) == 2 and all(row[2]["id"] == 1 for row in rows)
    start = len(observations)
    must_fail(
        lambda: scan(
            connection,
            [source("a", "SELECT id FROM work"), source("b", "SELECT private_note FROM work")],
        ),
        "permission denied",
    )
    assert len(observations) == start
    connection.execute("RESET ROLE")
    checks.append("every source retains invoker column permissions and row security")

    portals = [
        source(str(i), f"SELECT id,{i} AS marker FROM generate_series(1,100) id") for i in range(3)
    ]
    with connection.transaction():
        before = open_portals(connection)
        for projection in ("* FROM", ""):
            connection.execute(
                f"SELECT {projection} jev_native.scan_many(%s,'{{\"batch_rows\":1}}') LIMIT 1",
                (Jsonb(portals),),
            ).fetchall()
            assert open_portals(connection) == before
        connection.execute(
            "DECLARE multi_result CURSOR FOR SELECT jev_native.scan_many(%s,'{\"batch_rows\":1}')",
            (Jsonb(portals),),
        )
        connection.execute("FETCH 1 FROM multi_result").fetchall()
        connection.execute("CLOSE multi_result")
        assert open_portals(connection) == before
    connection.execute("SET statement_timeout='150ms'")
    must_fail(
        lambda: scan(
            connection, [source("a", "SELECT 2 AS delay"), source("b", "SELECT 2 AS delay,1 AS id")]
        ),
        "statement timeout",
    )
    connection.execute("RESET statement_timeout")
    assert open_portals(connection) == []
    checks.append("early stops, explicit cursor close and cancellation release every source portal")

    connection.execute("CREATE TABLE frontier_changes(id int, note text, p numeric)")
    connection.execute("INSERT INTO frontier_changes VALUES (1,'first',0.95),(2,'original',0.95)")
    gates["snapshot"] = threading.Event()
    changing = [
        source(
            "a",
            "SELECT id,'a' AS marker,'snapshot' AS gate FROM generate_series(1,2) id ORDER BY id",
        ),
        source("b", "SELECT *,'snapshot' AS gate FROM frontier_changes ORDER BY id"),
    ]

    def read_snapshot():
        with psycopg.connect(connection.info.dsn, autocommit=True) as reader:
            return scan(reader, changing, {"batch_rows": 2})

    start = len(observations)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(read_snapshot)
        try:
            deadline = time.monotonic() + 2
            while (
                len(observations) < start + 2 and not future.done() and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            assert len(observations) == start + 2
            connection.execute("UPDATE frontier_changes SET note='changed',p=0.05 WHERE id=2")
        finally:
            gates["snapshot"].set()
        rows = future.result(timeout=5)
    second = next(row for row in rows if row[0] == "b" and row[1] == 2)
    assert second[2]["note"] == "original" and second[3]["q"]["value"] is True
    checks.append("all source cursors retain one snapshot through writes between provider batches")

    timed = [source(str(i), f"SELECT {i} AS id,0.1 AS delay") for i in range(8)]
    start = len(observations)
    started = time.monotonic()
    for item in timed:
        scan(connection, [item])
    serial_seconds = time.monotonic() - started
    assert len(observations) == start + 8
    start = len(observations)
    started = time.monotonic()
    rows = scan(connection, timed, {"concurrency": 4})
    shared_seconds = time.monotonic() - started
    assert len(observations) == start + 8
    assert max(row[5]["requests"] for row in rows) == 8
    return checks, {
        "sources": 8,
        "fixture_delay_seconds": 0.1,
        "requests_each": 8,
        "sequential_seconds": serial_seconds,
        "shared_seconds": shared_seconds,
    }
