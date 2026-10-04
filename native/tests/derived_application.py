"""Verify automatic derived semantic SQL through the tenant-aware query service."""

from sqlalchemy import text

from sdd.generic.hybrid_feedback import checked_candidate


def verify_derived_application(service, catalog, tenant, observations):
    checks = []
    for table, note, definition in [
        ("work_items", "note", "Complete?"),
        ("renamed_source", "description", "Has the work finished?"),
        ("事项", "说明", "任务是否已经完成？"),
    ]:
        query = f'''WITH summary AS (
            SELECT "{note}" AS description,MAX(p) AS p FROM "{table}" GROUP BY "{note}"
        ) SELECT COUNT(*) AS n FROM summary WHERE SEMANTIC(description,'{definition}')'''
        checked = checked_candidate(
            service, tenant, {"sql": query}, {dataset["id"] for dataset in catalog.list(tenant)}
        )
        assert checked["valid"] and checked["backend_probe"]["output_state"] == "NOT_EVALUATED"
        start = len(observations)
        result = service.execute(tenant, query)
        assert result["result"] == [{"n": 2}], result
        assert len(observations) == start + 3
        assert result["manifest"]["native_scheduler"] == "dependent_stage_dag"
        assert result["manifest"]["result_output_state"] == "VALUE"
        assert result["manifest"]["evaluator"][0]["model"] == "fixture-v1"
        assert all("id" not in call["state"] for call in observations[start:])
    checks.append(
        "query service evaluates semantic predicates after SQL aggregation across renamed and Chinese schemas"
    )

    query = """WITH selected AS (
        SELECT note,p FROM work_items WHERE SEMANTIC(note,'Complete?')
    ), combined AS (
        SELECT STRING_AGG(note,'; ' ORDER BY note) AS description,MAX(p) AS p FROM selected
    ) SELECT SEMANTIC(description,'Does the summary describe finished work?') AS done FROM combined"""
    start = len(observations)
    result = service.execute(tenant, query)
    assert result["result"] == [{"done": True}], result
    assert len(observations) == start + 4
    assert observations[-1]["state"]["description"] == "O'Reilly 100% complete; 完成"
    assert observations[-1]["state"]["p"] > 0.9
    assert result["manifest"]["semantic_coverage"]["requests"] == 4
    checks.append(
        "dependent semantic SQL reuses one executor across filtering, PostgreSQL text aggregation and a second judgment"
    )

    result = service.execute(
        tenant,
        """WITH q AS (
        SELECT note,p,CAST('900719925474099312345.123456789' AS NUMERIC) AS amount
        FROM work_items WHERE id=1
    ) SELECT amount+1 AS amount FROM q WHERE SEMANTIC(note,'Complete?')""",
    )
    assert result["result"] == [{"amount": "900719925474099312346.123456789"}]
    with service.db.transaction(tenant) as connection:
        saved = connection.execute(
            text("SELECT result FROM dataset_query_runs WHERE id=:id"), {"id": result["run_id"]}
        ).scalar_one()
        assert saved == result["result"]
    checks.append(
        "derived native results retain exact PostgreSQL decimals in API output and saved history"
    )

    query = """WITH common AS (SELECT note,p FROM work_items),
        a AS (SELECT note FROM common WHERE SEMANTIC(note,'First rule?')),
        b AS (SELECT note FROM common WHERE SEMANTIC(note,'Second rule?'))
        SELECT count(*) AS n FROM a JOIN b ON a.note=b.note"""
    result = service.execute(tenant, query)
    assert result["result"] == [{"n": 2}]
    stages = result["manifest"]["stage_receipts"]
    assert sum(stage["operator"] == "semantic" for stage in stages) == 2
    assert result["manifest"]["materialized_rows"] == 14
    checks.append("automatic SQL lowering retains shared CTEs and independent semantic branches")

    held_query = "WITH q AS (SELECT note,p FROM work_items) SELECT count(*) AS n FROM q WHERE SEMANTIC(note,'Complete?')"
    start = len(observations)
    held = service.execute(tenant, held_query, max_evaluations=0)
    assert len(observations) == start and held["result"] == []
    assert held["manifest"]["result_output_state"] == "NOT_EVALUATED"
    assert held["manifest"]["result_operation_state"] == "BLOCKED_BY_DEPENDENCY"
    assert not held["manifest"]["complete"]
    assert held["manifest"]["semantic_coverage"]["not_evaluated"] == 3
    assert held["manifest"]["semantic_coverage"]["resolved"] == 0
    with service.db.transaction(tenant) as connection:
        saved = connection.execute(
            text("SELECT manifest FROM dataset_query_runs WHERE id=:id"), {"id": held["run_id"]}
        ).scalar_one()
        assert saved["result_output_state"] == "NOT_EVALUATED"
    checks.append(
        "held derived queries retain their SQL and stage receipts instead of returning a fabricated zero count"
    )

    unknown = catalog.create(
        tenant,
        "uncertain_native_scope",
        [{"id": 1, "note": "unclear", "p": 0.5}],
        primary_key=["id"],
    )
    held = service.execute(
        tenant,
        "WITH q AS (SELECT note,p FROM uncertain_native_scope) SELECT count(*) AS n FROM q WHERE SEMANTIC(note,'Complete?')",
    )
    assert held["manifest"]["result_output_state"] == "NOT_EVALUATED"
    assert held["manifest"]["semantic_coverage"]["unknown"] == 1
    assert any(
        sum(item["UNKNOWN"] for item in stage["decisions"].values())
        for stage in held["manifest"]["stage_receipts"]
    )
    start = len(observations)
    try:
        service.execute(
            "different-tenant",
            f"WITH q AS (SELECT note FROM {unknown['name']}) SELECT SEMANTIC(note,'Complete?') FROM q",
        )
    except ValueError as error:
        assert "unauthorized" in str(error)
    else:
        raise AssertionError("Derived route bypassed the tenant catalog")
    assert len(observations) == start
    checks.append(
        "uncertain derived membership holds population calculations and catalog authorization precedes dispatch"
    )
    return checks
