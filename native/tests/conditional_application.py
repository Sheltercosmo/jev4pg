"""Conditional SQL through catalog authorization, native execution and history."""

from sqlalchemy import text

from sdd.generic.hybrid_feedback import checked_candidate


def verify_conditional_application(service, catalog, tenant, observations):
    checks = []
    dataset = catalog.create(
        tenant,
        "conditional_items",
        [
            {"id": 1, "note": "ready", "p": 0.95, "flag": True},
            {"id": 2, "note": "pending", "p": 0.05, "flag": False},
            {"id": 3, "note": "uncertain", "p": 0.5, "flag": None},
            {"id": 4, "note": "ready", "p": 0.95, "flag": True},
            {"id": 5, "note": None, "p": 0.95, "flag": False},
        ],
        primary_key=["id"],
    )

    query = "SELECT id,CASE WHEN id=1 THEN SEMANTIC(note,'Complete?') ELSE false END AS answer FROM conditional_items ORDER BY id"
    candidate = checked_candidate(service, tenant, {"sql": query}, {dataset["id"]})
    assert candidate["valid"] and candidate["backend_probe"]["output_state"] == "NOT_EVALUATED", (
        candidate
    )
    start = len(observations)
    result = service.execute(tenant, query)
    assert result["result"] == [{"id": i, "answer": i == 1} for i in range(1, 6)], result
    assert len(observations) == start + 1 and observations[-1]["state"]["id"] == 1
    assert set(observations[-1]["state"]) == {"id", "note", "p", "flag"}
    assert result["manifest"]["complete"] and result["manifest"]["semantic_coverage"]["total"] == 5
    checks.append(
        "automatic CASE evaluates only the selected semantic row and excludes compiler routing fields from provider context"
    )

    for expression, expected, calls in [
        (
            "CASE WHEN flag THEN SEMANTIC(note,'Complete?') ELSE false END",
            [True, False, False, True, False],
            2,
        ),
        ("CASE WHEN id=1 THEN SEMANTIC(note,'Complete?') END", [True, None, None, None, None], 1),
        (
            "CASE id WHEN 1 THEN SEMANTIC(note,'First?') WHEN 2 THEN SEMANTIC(note,'Second?') ELSE false END",
            [True, False, False, False, False],
            2,
        ),
        (
            "CASE WHEN id<>3 THEN CASE WHEN flag THEN SEMANTIC(note,'First?') ELSE false END ELSE false END",
            [True, False, False, True, False],
            2,
        ),
    ]:
        start = len(observations)
        output = service.execute(
            tenant, f"SELECT {expression} AS answer FROM conditional_items ORDER BY id"
        )
        assert output["result"] == [{"answer": item} for item in expected], output
        assert len(observations) == start + calls
    checks.append(
        "searched, simple and nested CASE preserve SQL NULL fallthrough and omitted ELSE values while skipping irrelevant judgments"
    )

    for table, identity, note, definition in [
        ("work_items", "id", "note", "Complete?"),
        ("renamed_source", "key_value", "description", "Has the work finished?"),
        ("事项", "编号", "说明", "任务是否已经完成？"),
    ]:
        query = f'''SELECT "{identity}" AS id,CASE WHEN "{identity}"=1 THEN SEMANTIC("{note}",'{definition}') ELSE false END AS answer FROM "{table}" ORDER BY "{identity}"'''
        output = service.execute(tenant, query)
        assert output["result"] == [
            {"id": 1, "answer": True},
            {"id": 2, "answer": False},
            {"id": 3, "answer": False},
        ]
        assert output["manifest"]["semantic_coverage"]["requests"] == 1
    checks.append(
        "conditional query execution preserves behavior under schema renaming, English paraphrases and Simplified Chinese contracts"
    )

    query = """SELECT SEMANTIC(note,'Independent?') AS independent,
        CASE WHEN SEMANTIC(note,'Route?') THEN SEMANTIC(note,'Urgent?') ELSE SEMANTIC(note,'Routine?') END AS answer
        FROM conditional_items WHERE id IN(1,2) ORDER BY id"""
    start = len(observations)
    output = service.execute(tenant, query)
    assert output["result"] == [
        {"independent": True, "answer": True},
        {"independent": False, "answer": False},
    ], output
    assert output["manifest"]["semantic_coverage"]["requests"] == 4
    assert sorted(len(item["questions"]) for item in observations[start:]) == [1, 1, 2, 2]
    checks.append(
        "semantic CASE selectors batch independent questions and dispatch only the applicable downstream question for each row"
    )

    query = "SELECT CASE WHEN SEMANTIC(note,'Route?') THEN SEMANTIC(note,'Urgent?') ELSE SEMANTIC(note,'Routine?') END AS answer FROM conditional_items WHERE id=3"
    start = len(observations)
    held = service.execute(tenant, query)
    assert held["result"] == [] and held["manifest"]["result_output_state"] == "NOT_EVALUATED"
    assert len(observations) == start + 1
    assert (
        next(iter(observations[-1]["questions"].values()))["instructions"]["definition"] == "Route?"
    )
    with service.db.transaction(tenant) as connection:
        stored = connection.execute(
            text("SELECT manifest FROM sdd_catalog.dataset_query_runs WHERE id=:id"), {"id": held["run_id"]}
        ).scalar_one()
        assert stored["result_operation_state"] == "BLOCKED_BY_DEPENDENCY"
    checks.append(
        "an uncertain semantic CASE selector holds the proposed query and saved history without selecting ELSE"
    )

    for condition, expected in [
        ("false AND SEMANTIC(note,'Unneeded?')", False),
        ("SEMANTIC(note,'Unneeded?') AND false", False),
        ("true OR SEMANTIC(note,'Unneeded?')", True),
        ("NOT (false AND SEMANTIC(note,'Unneeded?'))", True),
    ]:
        start = len(observations)
        output = service.execute(
            tenant,
            f"SELECT CASE WHEN {condition} THEN true ELSE false END AS answer FROM conditional_items WHERE id=3",
            max_evaluations=0,
        )
        assert output["result"] == [{"answer": expected}] and len(observations) == start, output
    checks.append(
        "exact Boolean conditions eliminate unnecessary semantic work without requiring a model allowance or treating skipped work as false"
    )

    query = "SELECT CASE WHEN id=1 THEN SEMANTIC(note,'Complete?') ELSE false END AS answer FROM conditional_items WHERE id=1"
    held = service.execute(tenant, query, max_evaluations=0)
    assert held["result"] == [] and not held["manifest"]["complete"]
    output = service.execute(tenant, query.replace("WHERE id=1", "WHERE id=2"), max_evaluations=0)
    assert output["result"] == [{"answer": False}] and output["manifest"]["complete"]
    checks.append(
        "conditional completeness requires selected decisions while a query whose semantic branch is unused can complete with zero allowance"
    )

    query = """WITH projected AS (SELECT note,p,flag FROM conditional_items WHERE id IN(1,4))
        SELECT CASE WHEN flag THEN SEMANTIC(note,'Complete?') ELSE false END AS answer FROM projected"""
    start = len(observations)
    output = service.execute(tenant, query)
    assert output["result"] == [{"answer": True}, {"answer": True}]
    assert (
        len(observations) == start + 1
        and output["manifest"]["semantic_coverage"]["reused_rows"] == 1
    )
    output = service.execute(
        tenant,
        """WITH projected AS (SELECT note,p,flag FROM conditional_items WHERE id IN(1,4))
        SELECT SUM(CASE WHEN flag THEN CASE WHEN SEMANTIC(note,'Complete?') THEN 1 ELSE 0 END ELSE 0 END) AS n FROM projected""",
    )
    assert output["result"] == [{"n": 2}], output
    checks.append(
        "compiler row identity preserves duplicate multiplicity through conditional aggregation without defeating context reuse"
    )
    start = len(observations)
    empty = service.execute(
        tenant,
        "SELECT CASE WHEN flag THEN SEMANTIC(note,'Complete?') ELSE false END AS answer FROM conditional_items WHERE id<0",
    )
    assert empty["result"] == [] and empty["manifest"]["complete"], empty
    aggregate = service.execute(
        tenant,
        "SELECT SUM(CASE WHEN SEMANTIC(note,'Complete?') THEN 1 ELSE 0 END) AS n FROM conditional_items WHERE id<0",
    )
    assert aggregate["result"] == [{"n": None}] and aggregate["manifest"]["complete"], aggregate
    missing = service.execute(
        tenant,
        "SELECT CASE WHEN id=5 THEN SEMANTIC(note,'Complete?') ELSE false END AS answer FROM conditional_items WHERE id=5",
    )
    assert missing["result"] == [] and not missing["manifest"]["complete"], missing
    assert missing["manifest"]["semantic_coverage"]["not_evaluated"] == 1
    assert len(observations) == start
    checks.append(
        "empty conditional populations retain SQL aggregate semantics while a selected NULL subject remains unexecuted and holds the result"
    )
    return checks
