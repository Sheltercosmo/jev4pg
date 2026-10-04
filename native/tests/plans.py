"""Integration fixtures for native relational and semantic stage dependencies."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import sqlglot
from psycopg.types.json import Jsonb

from sdd.generic.native_plan import native_plan
from sdd.generic.stage_dag import Column, StageDAG, operation, ref


QUESTIONS = {"done": {"type": "noul", "instructions": "Has the work finished? 完成了吗？"}}


def stage(identity, sql, columns, inputs=(), *, semantic=False, guard=None):
    result = {
        "id": identity,
        "operator": "semantic" if semantic else "source",
        "sql": sql,
        "columns": {name: {"kind": kind, "label": name} for name, kind in columns.items()},
        "inputs": [
            item if isinstance(item, dict) else {"stage": item, "alias": item} for item in inputs
        ],
    }
    if semantic:
        result["questions"] = QUESTIONS
    if guard is not None:
        result["guard"] = guard
    return result


def execute(connection, stages, options=None):
    connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
    try:
        result = connection.execute(
            "SELECT jev_native.execute_plan(%s,%s)",
            (
                Jsonb({"version": 1, "target": stages[-1]["id"], "stages": stages}),
                Jsonb(options or {}),
            ),
        ).fetchone()[0]
        assert (
            connection.execute(
                "SELECT count(*) FROM pg_class WHERE relnamespace=pg_my_temp_schema() AND relname LIKE '__jev_plan_%'"
            ).fetchone()[0]
            == 0
        )
        return result
    finally:
        connection.execute("ROLLBACK")


def verify_plans(connection, observations, gates):
    checks = []
    source = stage(
        "assessed",
        "SELECT * FROM (VALUES ('完成',0.95::numeric),('完成',0.95),('ready',0.95),('pending',0.05)) s(note,p)",
        {"note": "text", "p": "number"},
        semantic=True,
    )
    selected = stage(
        "selected",
        "SELECT count(*) AS n FROM assessed WHERE jev_native.require_bool(__jev_decisions,'done')",
        {"n": "integer"},
        [{"stage": "assessed", "alias": "assessed", "require_values": ["done"]}],
    )
    population = stage(
        "population", "SELECT count(*) AS n FROM assessed", {"n": "integer"}, ["assessed"]
    )
    result = stage(
        "result",
        "SELECT s.n AS chosen,p.n AS total,s.n::numeric/p.n AS ratio FROM selected s CROSS JOIN population p",
        {"chosen": "integer", "total": "integer", "ratio": "number"},
        ["selected", "population"],
    )
    start = len(observations)
    output = execute(connection, [source, selected, population, result], {"batch_rows": 2})
    assert output["rows"] == [{"chosen": 3, "total": 4, "ratio": 0.75}]
    assert len(observations) == start + 3 and output["usage"]["requests"] == 3
    assert output["stages"][0]["rows"] == 4
    checks.append(
        "native shared diamond preserves duplicate membership and independent denominator grain"
    )

    review = stage(
        "review",
        "SELECT chosen,total,ratio FROM result",
        {"chosen": "integer", "total": "integer", "ratio": "number"},
        ["result"],
        semantic=True,
    )
    start = len(observations)
    output = execute(connection, [source, selected, population, result, review])
    assert output["usage"]["requests"] == 4
    assert observations[-1]["state"] == {"chosen": 3, "total": 4, "ratio": 0.75}
    assert len(observations) == start + 4
    checks.append(
        "downstream semantic review receives PostgreSQL aggregates and the independently scoped denominator"
    )

    uncertain = {**source, "sql": "SELECT 'unresolved'::text AS note,0.5::numeric AS p"}
    output = execute(connection, [uncertain, selected])
    assert output["rows"] == []
    assert output["stages"][-1]["output_state"] == "NOT_EVALUATED"
    assert output["stages"][-1]["operation_state"] == "BLOCKED_BY_DEPENDENCY"
    assert output["stages"][0]["decisions"]["done"] == {
        "VALUE": 0,
        "UNKNOWN": 1,
        "NOT_EVALUATED": 0,
    }
    empty = {**source, "sql": "SELECT NULL::text AS note,NULL::numeric AS p WHERE false"}
    output = execute(connection, [empty, selected])
    assert output["rows"] == [{"n": 0}] and output["stages"][0]["population_closed"]
    checks.append(
        "unresolved membership holds aggregates while a sealed empty population counts as zero"
    )

    scoped = stage(
        "scoped",
        'SELECT 1 AS id,\'{"other":{"type":"noul","noul":0.5}}\'::jsonb AS fixture_answers',
        {"id": "integer", "fixture_answers": "json"},
        semantic=True,
    )
    scoped["questions"] = {
        **QUESTIONS,
        "other": {"type": "noul", "instructions": "A separate question"},
    }
    consumer = stage("consumer", "SELECT id FROM scoped", {"id": "integer"}, ["scoped"])
    assert (
        execute(connection, [scoped, consumer])["stages"][-1]["operation_state"]
        == "BLOCKED_BY_DEPENDENCY"
    )
    consumer["inputs"][0]["require_values"] = ["done"]
    assert execute(connection, [scoped, consumer])["rows"] == [{"id": 1}]
    consumer["inputs"][0]["require_values"] = []
    assert execute(connection, [scoped, consumer])["rows"] == [{"id": 1}]
    checks.append(
        "default completeness protects consumers while explicit question scopes permit unrelated uncertainty"
    )

    for sql, options, comparison, expected in [
        ("SELECT 0.95::numeric AS p", {}, True, "SUCCEEDED"),
        ("SELECT 0.05::numeric AS p", {}, True, "SKIPPED"),
        ("SELECT 0.5::numeric AS p", {}, True, "BLOCKED_BY_DEPENDENCY"),
        ("SELECT 0.95::numeric AS p", {"max_requests": 0}, True, "BLOCKED_BY_DEPENDENCY"),
        ("SELECT 'invalid'::text AS p", {}, True, "BLOCKED_BY_DEPENDENCY"),
        ("SELECT 0.95::numeric AS p WHERE false", {}, True, "BLOCKED_BY_DEPENDENCY"),
        ("SELECT 0.95::numeric AS p FROM generate_series(1,2)", {}, True, "FAILED"),
        ("SELECT 0.95::numeric AS p", {}, 1, "BLOCKED_BY_POLICY"),
    ]:
        parent = stage(
            "parent", sql, {"p": "text" if "invalid" in sql else "number"}, semantic=True
        )
        child = stage(
            "child",
            "SELECT 7 AS answer",
            {"answer": "integer"},
            ["parent"],
            guard={"input": "parent", "question": "done", "equals": comparison},
        )
        output = execute(connection, [parent, child], options)
        assert output["stages"][-1]["operation_state"] == expected, output
        assert output["rows"] == ([{"answer": 7}] if expected == "SUCCEEDED" else [])
        assert output["stages"][-1]["output_state"] == (
            "VALUE" if expected == "SUCCEEDED" else "NOT_EVALUATED"
        )
    checks.append(
        "scalar guards distinguish false, unknown, missing, failed, budget-held, ambiguous and incompatible values"
    )

    fast = stage("fast", "SELECT 1 AS id", {"id": "integer"}, semantic=True)
    slow = stage(
        "slow", "SELECT generate_series(10,29)::int AS id", {"id": "integer"}, semantic=True
    )
    child = stage("child", "SELECT 101 AS id FROM fast", {"id": "integer"}, ["fast"], semantic=True)
    target = stage(
        "target",
        "SELECT count(*) AS n FROM slow CROSS JOIN child",
        {"n": "integer"},
        ["slow", "child"],
    )
    start = len(observations)
    output = execute(connection, [fast, slow, child, target], {"batch_rows": 2, "concurrency": 2})
    calls = [call["state"]["id"] for call in observations[start:]]
    assert calls.index(101) < calls.index(29), calls
    assert output["completion_order"].index("child") < output["completion_order"].index("slow")
    assert output["rows"] == [{"n": 20}] and output["usage"]["requests"] == 22
    output = execute(connection, [fast, slow, child, target], {"batch_rows": 2, "max_requests": 2})
    assert output["usage"]["requests"] == 2
    assert (
        next(s for s in output["stages"] if s["id"] == "child")["decisions"]["done"][
            "NOT_EVALUATED"
        ]
        == 1
    )
    checks.append(
        "dependent native work starts before an independent population finishes and retains one request allowance"
    )

    dag = StageDAG()
    first = dag.source(
        sqlglot.parse_one(
            "SELECT team,amount FROM (VALUES ('甲',10::numeric),('甲',20),('乙',9)) s(team,amount)",
            read="postgres",
        ),
        {"team": Column("text", "部门"), "amount": Column("number", "总额")},
    )
    totals = dag.aggregate(first, ["team"], {"total": operation("sum", ref("amount"))})
    ranked = dag.window(totals, "position", operation("rank"), order=(("total", True),))
    final = dag.aggregate(ranked, [], {"maximum": operation("max", ref("total"))})
    plan = native_plan(dag, final)
    output = execute(connection, plan["stages"])
    assert output["rows"] == [{"maximum": 30}] and output["usage"]["requests"] == 0
    checks.append(
        "existing typed StageDAG lowers aggregate-window-aggregate SQL into native PostgreSQL execution"
    )

    ordered = stage(
        "ordered",
        "SELECT id AS z,id+10 AS a FROM generate_series(1,5) id ORDER BY id DESC",
        {"z": "integer", "a": "integer"},
    )
    positional = stage(
        "positional",
        "SELECT * FROM ordered UNION ALL SELECT 0,10 ORDER BY z DESC",
        {"z": "integer", "a": "integer"},
        ["ordered"],
    )
    assert execute(connection, [ordered, positional])["rows"] == [
        {"z": i, "a": i + 10} for i in range(5, -1, -1)
    ]
    checks.append(
        "materialization preserves PostgreSQL column positions and explicit target ordering"
    )

    for name, text in [
        ("note", "Is it complete?"),
        ("description", "Has the work finished?"),
        ("说明", "是否已经完成？"),
    ]:
        parent = stage(
            "父",
            f"SELECT '完成'::text AS \"{name}\",900719925474099312345.123456789::numeric AS amount",
            {name: "text", "amount": "number"},
            semantic=True,
        )
        parent["questions"] = {"done": {"type": "noul", "instructions": text}}
        child = stage(
            "子", 'SELECT (amount+1::numeric)::text AS amount FROM "父"', {"amount": "text"}, ["父"]
        )
        start = len(observations)
        output = execute(connection, [parent, child])
        assert str(observations[start]["state"]["amount"]) == "900719925474099312345.123456789"
        assert output["stages"][-1]["population_closed"]
        assert output["rows"][0]["amount"] == "900719925474099312346.123456789"
    checks.append(
        "renamed and Simplified Chinese stage contracts preserve exact numeric inputs and PostgreSQL arithmetic"
    )

    from postgres import must_fail

    before = len(observations)
    for bad in [
        stage("bad", "SELECT 1 AS id; DELETE FROM work", {"id": "integer"}, semantic=True),
        stage(
            "bad",
            "WITH changed AS (DELETE FROM work RETURNING id) SELECT id FROM changed",
            {"id": "integer"},
            semantic=True,
        ),
        stage("bad", "SELECT NULL::integer AS id", {"id": "text"}, semantic=True),
    ]:
        must_fail(lambda: execute(connection, [bad]))
    must_fail(lambda: execute(connection, [slow], {"max_rows": 1}), "max_rows")
    must_fail(
        lambda: connection.execute(
            "SELECT jev_native.execute_plan(%s)",
            (Jsonb({"version": 1, "target": "fast", "stages": [fast]}),),
        ),
        "REPEATABLE READ",
    )
    assert len(observations) == before
    checks.append(
        "plan source shape, SQL statements, isolation and intermediate limits fail before provider dispatch"
    )

    connection.execute("CREATE TABLE plan_snapshot(id int)")
    connection.execute("INSERT INTO plan_snapshot VALUES(1)")
    gates["plan_snapshot"] = threading.Event()
    parent = stage(
        "parent",
        "SELECT 'plan_snapshot'::text AS gate,id FROM plan_snapshot",
        {"gate": "text", "id": "integer"},
        semantic=True,
    )
    child = stage(
        "child",
        "SELECT p.id AS before,s.id AS after FROM parent p CROSS JOIN plan_snapshot s",
        {"before": "integer", "after": "integer"},
        ["parent"],
    )

    def run_snapshot():
        with psycopg.connect(connection.info.dsn, autocommit=True) as client:
            return execute(client, [parent, child])

    start = len(observations)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_snapshot)
        deadline = time.monotonic() + 5
        while len(observations) == start and not future.done() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(observations) == start + 1
        connection.execute("UPDATE plan_snapshot SET id=2")
        gates["plan_snapshot"].set()
        assert future.result(timeout=10)["rows"] == [{"before": 1, "after": 1}]
    checks.append(
        "dependent stages see temporary writes while retaining one external source snapshot"
    )

    connection.execute("CREATE ROLE native_plan_reader")
    connection.execute("GRANT USAGE ON SCHEMA jev_native TO native_plan_reader")
    connection.execute("CREATE TABLE plan_private(id int, note text, secret text)")
    connection.execute(
        "INSERT INTO plan_private VALUES(1,'visible','private'),(2,'hidden','private')"
    )
    connection.execute("ALTER TABLE plan_private ENABLE ROW LEVEL SECURITY")
    connection.execute("CREATE POLICY plan_visibility ON plan_private USING(id=1)")
    connection.execute("GRANT SELECT(id,note) ON plan_private TO native_plan_reader")
    reader_plan = stage(
        "rows", "SELECT id,note FROM plan_private", {"id": "integer", "note": "text"}, semantic=True
    )
    connection.execute("SET ROLE native_plan_reader")
    try:
        must_fail(lambda: execute(connection, [reader_plan]), "permission denied")
    finally:
        connection.execute("RESET ROLE")
    connection.execute(
        "GRANT EXECUTE ON FUNCTION jev_native.execute_plan(jsonb,jsonb) TO native_plan_reader"
    )
    connection.execute("SET ROLE native_plan_reader")
    try:
        rows = execute(connection, [reader_plan])["rows"]
        assert len(rows) == 1 and rows[0]["id"] == 1
        assert rows[0]["__jev_decisions"]["done"]["value"] is True
        denied = stage("rows", "SELECT secret FROM plan_private", {"secret": "text"}, semantic=True)
        must_fail(lambda: execute(connection, [denied]), "permission denied")
    finally:
        connection.execute("RESET ROLE")
    checks.append(
        "native graph execution enforces explicit grants, source column privileges and row security"
    )

    gates["plan_cancel"] = threading.Event()
    waiting = stage(
        "waiting", "SELECT 'plan_cancel'::text AS gate", {"gate": "text"}, semantic=True
    )
    backend = []

    def run_cancelled():
        with psycopg.connect(connection.info.dsn, autocommit=True) as client:
            backend.append(client.info.backend_pid)
            must_fail(lambda: execute(client, [waiting]), "cancel")
            assert (
                client.execute(
                    "SELECT count(*) FROM pg_class WHERE relnamespace=pg_my_temp_schema() AND relname LIKE '__jev_plan_%'"
                ).fetchone()[0]
                == 0
            )
            assert (
                client.execute(
                    "SELECT count(*) FROM pg_cursors WHERE statement LIKE '%__jev_plan_%'"
                ).fetchone()[0]
                == 0
            )

    start = len(observations)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_cancelled)
        try:
            deadline = time.monotonic() + 5
            while len(observations) == start and not future.done() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(observations) == start + 1
            assert connection.execute("SELECT pg_cancel_backend(%s)", (backend[0],)).fetchone()[0]
            future.result(timeout=5)
        finally:
            gates["plan_cancel"].set()
    checks.append(
        "cancellation releases native graph cursors and transactional intermediate relations"
    )
    return checks


def verify_plan_replay(connection, observations):
    parent = stage(
        "parent",
        "SELECT 'plan-policy'::text AS note,0.7::numeric AS p",
        {"note": "text", "p": "number"},
        semantic=True,
    )
    child = stage(
        "child",
        "SELECT 11 AS answer",
        {"answer": "integer"},
        ["parent"],
        guard={"input": "parent", "question": "done", "equals": True},
    )
    options = {"evidence_scope": "plan-policy-replay"}
    start = len(observations)
    original = execute(connection, [parent, child], options)
    assert (
        original["rows"] == []
        and original["stages"][-1]["operation_state"] == "BLOCKED_BY_DEPENDENCY"
    )
    replay = execute(
        connection,
        [parent, child],
        {**options, "max_requests": 0, "accept": 0.6, "policy_revision": "plan-review-v2"},
    )
    assert replay["rows"] == [{"answer": 11}]
    assert replay["usage"]["requests"] == 0 and replay["usage"]["durable_reused_rows"] == 1
    assert len(observations) == start + 1
    return [
        "a revised policy unlocks a dependent native branch through durable evidence without new requests"
    ]
