"""Execute authorized derived semantic SQL through the native stage interface."""

import json
import os
import time
from collections import Counter
from decimal import Decimal

import psycopg
from sqlalchemy import text

from .native_admission import NativeAdmission
from .native_relational import compile_relational_plan
from .catalog import serial
from .source_catalog import source_transaction


def execute_relational(
    service,
    tenant,
    sql,
    tree,
    bindings,
    datasets,
    *,
    request,
    plan,
    max_evaluations,
    accept,
    reject,
    started,
    progress,
):
    max_rows = int(os.getenv("SDD_NATIVE_MAX_ROWS", "100000"))
    timeout_ms = int(os.getenv("SDD_NATIVE_TIMEOUT_MS", "120000"))
    if not 1 <= max_rows <= 1_000_000 or not 1 <= timeout_ms <= 600_000:
        raise ValueError("Invalid native source or statement limits")
    with (
        NativeAdmission.reserve(service.db, tenant, max_evaluations) as admission,
        source_transaction(service.db, tenant, datasets, "REPEATABLE READ") as connection,
    ):
        service.configure_transaction(connection)
        connection.execute(
            text("SELECT set_config('statement_timeout',:timeout,true)"),
            {"timeout": str(timeout_ms)},
        )
        available = connection.execute(
            text("SELECT to_regprocedure('jev_native.execute_plan(jsonb,jsonb)') IS NOT NULL")
        ).scalar_one()
        if not available:
            raise ValueError(
                "Install the native stage extension before querying derived semantic relations"
            )
        snapshot = connection.execute(text("SELECT pg_current_snapshot()::text")).scalar_one()

        def render(query):
            compiled, parameters = service.bind(query, bindings)
            statement = text(compiled).compile(dialect=connection.dialect)
            with psycopg.ClientCursor(connection.connection.driver_connection) as cursor:
                return cursor.mogrify(str(statement), parameters)

        graph = compile_relational_plan(tree, bindings, render)
        target = graph["stages"][-1]
        target["sql"] = "SELECT * FROM (" + target["sql"] + ") AS _sdd_result LIMIT 1001"
        options = {
            "evidence_scope": tenant,
            "max_rows": max_rows,
            "max_judgments": max_evaluations,
            "max_requests": admission.reserved,
            "max_input_bytes": 8_000_000,
            "accept": accept,
            "reject": reject,
            "concurrency": int(os.getenv("SDD_NATIVE_CONCURRENCY", "4")),
        }
        if progress is not None and not progress():
            raise ValueError("Query cancelled before native semantic dispatch")
        statement = """WITH execution AS MATERIALIZED (
            SELECT jev_native.execute_plan(CAST(:graph AS jsonb),CAST(:options AS jsonb)) AS output
        ) SELECT output - 'rows' AS metadata, (output -> 'rows')::text AS rows FROM execution"""
        parameters = {
            "graph": json.dumps(graph, ensure_ascii=False),
            "options": json.dumps(options),
        }
        admission.inflight = True
        response = connection.execute(text(statement), parameters).mappings().one()
        output = response["metadata"]
        admission.requests = output["usage"]["requests"]
        admission.inflight = False
        target_state = next(stage for stage in output["stages"] if stage["id"] == graph["target"])
        complete = target_state["output_state"] == "VALUE"
        rows = serial(json.loads(response["rows"], parse_float=Decimal))
        states = Counter()
        for stage in output["stages"]:
            if stage["operator"] != "semantic":
                continue
            for counts in stage["decisions"].values():
                states.update(counts)
        coverage = {
            **output["usage"],
            "total": sum(states.values()),
            "resolved": states["VALUE"],
            "unknown": states["UNKNOWN"],
            "not_evaluated": states["NOT_EVALUATED"],
            "unresolved": states["UNKNOWN"] + states["NOT_EVALUATED"],
            "new_evaluations": output["usage"]["judgments"],
        }
        manifest = {
            "execution_backend": "rust_postgresql",
            "native_scheduler": "dependent_stage_dag",
            "admission_id": admission.identity,
            "dataset_ids": [dataset["id"] for dataset in datasets],
            "source_snapshot": snapshot,
            "snapshot_mode": "postgres_repeatable_read",
            "source_rows": None,
            "source_rows_state": "NOT_EVALUATED",
            "semantic_snapshot": None,
            "semantic_snapshot_state": "NOT_EVALUATED",
            "stage_receipts": output["stages"],
            "stage_completion_order": output["completion_order"],
            "materialized_rows": output["materialized_rows"],
            "semantic_coverage": coverage,
            "evidence_receipts": output.get("evidence_receipts", []),
            "evaluator": output.get("evaluators", []),
            "evidence_retention": "native_registry"
            if output["usage"]["stored_observations"] or output["usage"]["durable_reused_rows"]
            else "coverage_summary",
            "decision_policy": output["policy"],
            "complete": complete,
            "result_output_state": target_state["output_state"],
            "result_operation_state": target_state["operation_state"],
            "result_hold_reason": target_state["reason"],
            "result_is_partial": not complete,
            "truncated": len(rows) > 1000,
            "operation": type(tree).__name__.lower(),
            "planning_ms": (plan or {}).get("planning_ms", 0),
            "execution_ms": round((time.perf_counter() - started) * 1000, 2),
            "execution_steps": [{"sql": statement, "parameters": parameters}],
        }
        return service.save(
            connection, tenant, request, sql, statement, parameters, plan, manifest, rows[:1000]
        )
