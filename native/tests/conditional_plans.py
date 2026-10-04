"""Conditional native execution, typed branch selection and shared admission."""

import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg

from plans import execute, stage


def value(item):
    return {"output_state": "VALUE", "operation_state": "SUCCEEDED", "value": item, "raw": {}}


def literal(item):
    return "'" + json.dumps(item, ensure_ascii=False).replace("'", "''") + "'::jsonb"


def branches(source, *, label="route", gate=None):
    plans = [source]
    for identity, selected in (("yes_branch", True), ("no_branch", False)):
        projection = "id,note,0.95::numeric AS p," + (
            f"__jev_decisions->'done' AS \"{label}\"" if source.get("questions") else f'"{label}"'
        )
        columns = {"id": "integer", "note": "text", "p": "number", label: "json"}
        if gate:
            projection += f",'{gate}'::text AS gate"
            columns["gate"] = "text"
        branch = stage(
            identity,
            f"SELECT {projection} FROM {source['id']}",
            columns,
            [{"stage": source["id"], "alias": source["id"], "require_values": []}],
            semantic=True,
        )
        branch["questions"] = {
            identity: {"type": "noul", "instructions": f"Review the {identity} route."}
        }
        branch["row_guard"] = {"column": label, "equals": selected}
        plans.append(branch)
    merge = stage(
        "merged",
        f'''SELECT y.id,y."{label}" AS selector,
        y.__jev_decisions->'yes_branch' AS yes_result,n.__jev_decisions->'no_branch' AS no_result
        FROM yes_branch y JOIN no_branch n USING(id)''',
        {"id": "integer", "selector": "json", "yes_result": "json", "no_result": "json"},
        [
            {"stage": name, "alias": name, "require_values": []}
            for name in ("yes_branch", "no_branch")
        ],
    )
    merge["operator"] = "merge"
    merge["keys"] = [["id"]]
    merge["selections"] = {
        "answer": {
            "selector": "selector",
            "cases": [
                {"equals": True, "column": "yes_result"},
                {"equals": False, "column": "no_result"},
            ],
        }
    }
    plans.append(merge)
    return plans


def inspect_rows(plans):
    return [
        *plans,
        stage(
            "visible",
            "SELECT id,__jev_decisions->'answer' AS answer FROM merged ORDER BY id",
            {"id": "integer", "answer": "json"},
            [{"stage": "merged", "alias": "merged", "require_values": []}],
        ),
    ]


def exact_count(plans):
    return [
        *plans,
        stage(
            "total",
            "SELECT count(*) AS n FROM merged WHERE jev_native.require_bool(__jev_decisions,'answer')",
            {"n": "integer"},
            ["merged"],
        ),
    ]


def verify_conditionals(connection, observations, gates):
    checks = []
    source = stage(
        "classified",
        """SELECT * FROM (VALUES
        (1,'ready',0.95::numeric),(2,'pending',0.05),(3,'uncertain',0.5),(4,NULL,0.95)
        ) s(id,note,p)""",
        {"id": "integer", "note": "text", "p": "number"},
        semantic=True,
    )
    source["questions"]["done"]["subject_column"] = "note"
    plans = branches(source)
    start = len(observations)
    output = execute(connection, inspect_rows(plans))
    assert [row["id"] for row in output["rows"]] == [1, 2, 3, 4]
    assert all(row["answer"]["value"] for row in output["rows"][:2])
    assert all(
        row["answer"]["output_state"] == "NOT_EVALUATED"
        and row["answer"]["operation_state"] == "BLOCKED_BY_DEPENDENCY"
        for row in output["rows"][2:]
    )
    assert output["usage"]["requests"] == 5 and len(observations) == start + 5
    for name in ("yes_branch", "no_branch"):
        receipt = next(item for item in output["stages"] if item["id"] == name)
        assert receipt["rows"] == 4 and receipt["population_closed"]
        assert receipt["decisions"][name] == {"VALUE": 1, "UNKNOWN": 0, "NOT_EVALUATED": 3}
    calls = [call for call in observations[start:] if "done" not in call["questions"]]
    assert sorted(call["state"]["id"] for call in calls) == [1, 2]
    assert all("route" not in call["state"] for call in calls)
    held = execute(connection, exact_count(plans))
    assert held["rows"] == [] and held["stages"][-1]["operation_state"] == "BLOCKED_BY_DEPENDENCY"
    checks.append(
        "row conditions retain unknown and skipped rows while dispatching only selected contexts; unresolved selections hold exact aggregates"
    )

    known = stage(
        "known",
        f"SELECT * FROM (VALUES (1,'a',{literal(value(True))}),(2,'b',{literal(value(False))})) s(id,note,route)",
        {"id": "integer", "note": "text", "route": "json"},
    )
    for label, question in (
        ("route", "Check the chosen record."),
        ("路由", "检查选中的记录。"),
        ("classification", "Does this record need review?"),
    ):
        renamed = copy.deepcopy(known)
        renamed["sql"] = known["sql"].replace("s(id,note,route)", f's(id,note,"{label}")')
        renamed["columns"][label] = renamed["columns"].pop("route")
        program = branches(renamed, label=label)
        for branch in program[1:3]:
            next(iter(branch["questions"].values()))["instructions"] = question
        start = len(observations)
        output = execute(connection, exact_count(program))
        assert output["rows"] == [{"n": 2}]
        assert output["usage"]["requests"] == 2 and len(observations) == start + 2
    checks.append(
        "selected branch composition and its exact aggregate work across renamed and Simplified Chinese contracts without a merge model call"
    )

    output = execute(
        connection, inspect_rows(branches(known)), {"max_requests": 1, "batch_rows": 2}
    )
    assert output["usage"]["requests"] == 1
    answers = [row["answer"] for row in output["rows"]]
    assert sum(answer["output_state"] == "VALUE" for answer in answers) == 1
    assert sum(answer["operation_state"] == "BLOCKED_BY_BUDGET" for answer in answers) == 1
    held = execute(connection, exact_count(branches(known)), {"max_requests": 1, "batch_rows": 2})
    assert held["rows"] == [] and not held["stages"][-1]["population_closed"]
    checks.append(
        "conditional branches retain shared query admission and propagate the selected branch's budget hold"
    )

    gate = "conditional_parallel"
    gates[gate] = threading.Event()
    start = len(observations)

    def run():
        with psycopg.connect(connection.info.dsn, autocommit=True) as other:
            return execute(
                other, exact_count(branches(known, gate=gate)), {"batch_rows": 4, "concurrency": 2}
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        try:
            deadline = time.monotonic() + 2
            while len(observations) < start + 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(observations) == start + 2 and not future.done()
            assert {next(iter(item["questions"])) for item in observations[start:]} == {
                "yes_branch",
                "no_branch",
            }
        finally:
            gates[gate].set()
        assert future.result(timeout=10)["rows"] == [{"n": 2}]
    checks.append(
        "independent row branches reach the provider concurrently before either response is released"
    )

    first, second = value(True), value(True)
    first["raw"], second["raw"] = {"p": 0.9}, {"p": 0.99}
    duplicate = stage(
        "duplicate",
        f"SELECT 'same'::text AS note,route FROM (VALUES ({literal(first)}),({literal(second)})) s(route)",
        {"note": "text", "route": "json"},
        semantic=True,
    )
    duplicate["row_guard"] = {"column": "route", "equals": True}
    start = len(observations)
    output = execute(connection, [duplicate], {"batch_rows": 1})
    assert (
        len(output["rows"]) == 2
        and output["usage"]["requests"] == 1
        and output["usage"]["reused_rows"] == 1
    )
    assert len(observations) == start + 1 and observations[-1]["state"] == {"note": "same"}
    assert output["rows"][0]["route"] != output["rows"][1]["route"]
    assert (
        connection.execute(
            "SELECT jev_native.decide(%s,%s)",
            (
                psycopg.types.json.Jsonb({"note": "same"}),
                psycopg.types.json.Jsonb(output["rows"][0]["__jev_observation"]),
            ),
        ).fetchone()[0]["done"]["value"]
        is True
    )
    checks.append(
        "row conditions keep duplicate multiplicity and reuse identical selected contexts without routing metadata or fabricated observations"
    )

    for raw, expected in (
        (None, "FAILED"),
        (value(1), "BLOCKED_BY_POLICY"),
        (value(False), "SKIPPED"),
    ):
        failed = stage(
            "failed",
            f"SELECT 'sample'::text AS note,{literal(raw)} AS route",
            {"note": "text", "route": "json"},
            semantic=True,
        )
        failed["row_guard"] = {"column": "route", "equals": True}
        start = len(observations)
        output = execute(connection, [failed])
        result = output["rows"][0]["__jev_decisions"]["done"]
        assert result["output_state"] == "NOT_EVALUATED" and result["operation_state"] == expected
        assert output["rows"][0]["__jev_observation"] is None and len(observations) == start
    empty = copy.deepcopy(duplicate)
    empty["sql"] = "SELECT NULL::text AS note,NULL::jsonb AS route WHERE false"
    output = execute(connection, [empty])
    assert (
        output["rows"] == []
        and output["stages"][0]["population_closed"]
        and output["usage"]["requests"] == 0
    )
    checks.append(
        "invalid, mismatched, unselected and empty row conditions never dispatch provider work or invent false values"
    )
    example = Path(__file__).resolve().parents[2] / "examples/planning/conditional_plan.sql"
    plan = json.loads(example.read_text(encoding="utf-8").split("$plan$")[1])
    start = len(observations)
    output = execute(connection, plan["stages"])
    assert [row["id"] for row in output["rows"]] == [1, 2]
    assert all(row["decision"]["output_state"] == "VALUE" for row in output["rows"])
    assert output["usage"]["requests"] == 2 and len(observations) == start + 2
    checks.append(
        "the documented conditional SQL example executes with one chosen review per source row"
    )
    return checks
