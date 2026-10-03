"""Compile semantic reads into PostgreSQL relations evaluated by the Rust extension."""

import json
import os
import time
from collections import Counter

import psycopg
import sqlglot
from sqlglot import exp
from sqlalchemy import text

from ..ledger import uid
from .catalog import serial
from .native_admission import NativeAdmission


def _conjuncts(node):
    if isinstance(node, exp.Paren):
        return _conjuncts(node.this)
    if isinstance(node, exp.And):
        return _conjuncts(node.this) + _conjuncts(node.expression)
    return [node]


def _allows_partial_results(tree, operators):
    """Prove a row-local fragment where unresolved values cannot invent membership."""
    semantic = {id(operator[0]) for operator in operators}

    def has_semantic(node):
        return any(id(child) in semantic for child in node.walk())

    def predicate(node):
        if id(node) in semantic or not has_semantic(node):
            return True
        if isinstance(node, (exp.Paren, exp.Not)):
            return predicate(node.this)
        if isinstance(node, (exp.And, exp.Or)):
            return predicate(node.this) and predicate(node.expression)
        if isinstance(node, (exp.EQ, exp.NEQ)):
            left, right = node.this, node.expression
            return (isinstance(right, exp.Boolean) and predicate(left)) or (
                isinstance(left, exp.Boolean) and predicate(right)
            )
        return False

    if not isinstance(tree, exp.Select):
        return False
    if any(
        value and key not in {"expressions", "from_", "joins", "where"}
        for key, value in tree.args.items()
    ):
        return False
    if any(
        isinstance(node, (exp.Query, exp.AggFunc, exp.Window)) and node is not tree
        for node in tree.walk()
    ):
        return False
    for projection in tree.expressions:
        while isinstance(projection, (exp.Alias, exp.Paren)):
            projection = projection.this
        if has_semantic(projection) and id(projection) not in semantic:
            return False
    for join in tree.args.get("joins", []):
        if join.side or join.kind not in {"", "INNER", "CROSS"}:
            return False
        if join.args.get("on") is not None and not predicate(join.args["on"]):
            return False
    where = tree.args.get("where")
    return where is None or predicate(where.this)


def _validate_semantic_lineage(operators):
    def joined(left, join):
        aliases, nullable = left
        right, right_nullable = relation(join.this)
        if join.side in {"RIGHT", "FULL"}:
            nullable |= aliases
        if join.side in {"LEFT", "FULL"}:
            right_nullable |= right
        return aliases | right, nullable | right_nullable

    def relation(node):
        if isinstance(node, exp.Subquery) and not isinstance(node.this, exp.Query):
            aliases, nullable = relation(node.this)
            if node.alias:
                aliases = {node.alias}
                nullable = {node.alias} if nullable else set()
        elif isinstance(node, (exp.Table, exp.Subquery)):
            aliases, nullable = {node.alias_or_name}, set()
        else:
            raise ValueError("Native semantic evaluation requires explicit table lineage")
        for join in node.args.get("joins", []):
            aliases, nullable = joined((aliases, nullable), join)
        return aliases, nullable

    for _, _, alias, _, scope in operators:
        group = scope.args.get("group")
        if group is not None and (
            group.find(exp.Rollup, exp.Cube, exp.GroupingSets)
            or any(value and key != "expressions" for key, value in group.args.items())
        ):
            raise ValueError(
                "Native semantic evaluation requires ordinary GROUP BY expressions; "
                "evaluate the base source in a CTE before grouping sets or subtotals"
            )
        source = scope.args.get("from_")
        lineage = relation(source.this) if source else (set(), set())
        for join in scope.args.get("joins", []):
            lineage = joined(lineage, join)
        if alias in lineage[1]:
            raise ValueError(
                "Semantic evaluation on a NULL-extended outer-join source is not supported; "
                "evaluate the base source in a CTE before the outer join"
            )


def _source_query(dataset, operators, bindings):
    source = exp.table_(dataset["name"], quoted=True).as_("_source", quoted=True)
    names = list(dict.fromkeys([*(c["name"] for c in dataset["columns"]), *dataset["primary_key"]]))
    query = exp.select(*(exp.column(name, table="_source", quoted=True) for name in names)).from_(
        source
    )
    query = query.order_by(
        *(exp.column(key, table="_source", quoted=True) for key in dataset["primary_key"])
    )
    branches = []
    visited = set()
    stable = {
        "AND",
        "OR",
        "NOT",
        "LOWER",
        "UPPER",
        "LENGTH",
        "COALESCE",
        "NULLIF",
        "ABS",
        "ROUND",
        "CAST",
    }
    for _, _, alias, _, scope in operators:
        if id(scope) in visited:
            continue
        visited.add(id(scope))
        tables = [node for node in scope.find_all(exp.Table) if id(node) in bindings]
        if (
            len(tables) != 1
            or scope.find(exp.Join, exp.Subquery, exp.Exists)
            or not scope.args.get("where")
        ):
            return query, source
        clauses = []
        for clause in _conjuncts(scope.args["where"].this):
            if clause.find(exp.Subquery, exp.AggFunc, exp.Window):
                continue
            if any(column.table != alias for column in clause.find_all(exp.Column)):
                continue
            functions = [
                f.name.upper() if isinstance(f, exp.Anonymous) else f.sql_name().upper()
                for f in clause.find_all(exp.Func)
            ]
            if any(name not in stable for name in functions):
                continue
            copied = clause.copy()
            for column in copied.find_all(exp.Column):
                column.set("table", exp.to_identifier("_source", quoted=True))
            clauses.append(copied)
        if not clauses:
            return query, source
        branches.append(exp.and_(*clauses))
    query.set("where", exp.Where(this=exp.or_(*branches)))
    return query, source


def _source_groups(operators, bindings):
    by_question = {}
    for operator in operators:
        by_question.setdefault((operator[1]["id"], operator[3].key), []).append(operator)
    groups = {}
    for group in by_question.values():
        dataset = group[0][1]
        query, source = _source_query(dataset, group, bindings)
        identity = (dataset["id"], query.sql(dialect="postgres"))
        if identity not in groups:
            groups[identity] = (dataset, query, source, [])
        groups[identity][3].extend(group)
    return groups.values()


def _source_text(service, connection, dataset, query, source):
    sql, parameters = service.bind(query, {id(source): dataset})
    statement = text(sql).compile(dialect=connection.dialect)
    with psycopg.ClientCursor(connection.connection.driver_connection) as cursor:
        return cursor.mogrify(str(statement), parameters)


def _usage(connection, relation):
    usage = (
        connection.execute(
            text(f"""
        SELECT count(*) AS source_rows,
               coalesce(max((usage->>'requests')::bigint),0) AS requests,
               coalesce(max((usage->>'judgments')::bigint),0) AS new_evaluations,
               coalesce(max((usage->>'input_bytes')::bigint),0) AS input_bytes,
               coalesce(max((usage->>'reused_rows')::bigint),0) AS reused_rows,
               coalesce(max((usage->>'durable_reused_rows')::bigint),0) AS durable_reused_rows,
               coalesce(max((usage->>'stored_observations')::bigint),0) AS stored_observations
        FROM {relation}
    """)
        )
        .mappings()
        .one()
    )
    return dict(usage)


def _coverage(connection, relation, source_id):
    by_question = {}
    for row in connection.execute(
        text(f"""
        SELECT q.key,
               count(*) AS total,
               count(*) FILTER (WHERE q.value->>'output_state' = 'VALUE') AS resolved,
               count(*) FILTER (WHERE q.value->>'output_state' = 'UNKNOWN') AS unknown,
               count(*) FILTER (WHERE q.value->>'output_state' = 'NOT_EVALUATED') AS not_evaluated,
               count(*) FILTER (WHERE q.value->>'operation_state' = 'FAILED') AS failed,
               count(*) FILTER (WHERE q.value->>'operation_state' = 'BLOCKED_BY_BUDGET') AS budget_skipped,
               count(*) FILTER (WHERE q.value->>'operation_state' = 'SKIPPED') AS missing_subject
        FROM {relation} AS r CROSS JOIN LATERAL jsonb_each(r.decisions) AS q
        WHERE r.source_id=:source_id
        GROUP BY q.key
    """),
        {"source_id": source_id},
    ).mappings():
        stats = dict(row)
        key = stats.pop("key")
        stats["unresolved"] = stats["unknown"] + stats["not_evaluated"]
        by_question[key] = stats
    return by_question


def _replace(operators, relations):
    for function, dataset, alias, spec, _ in operators:
        relation, source_id = relations[dataset["id"], spec.key]
        inner = "_decision_" + uid()

        def quote(value):
            return exp.to_identifier(value, quoted=True).sql(dialect="postgres")

        tests = [
            f"{quote(inner)}.source_id = {exp.Literal.string(source_id).sql(dialect='postgres')}"
        ] + [
            f"{quote(inner)}.source -> {exp.Literal.string(key).sql(dialect='postgres')} = "
            f"to_jsonb({exp.column(key, table=alias, quoted=True).sql(dialect='postgres')})"
            for key in dataset["primary_key"]
        ]
        replacement = sqlglot.parse_one(
            f"(SELECT ({quote(inner)}.decisions -> '{spec.key}' ->> 'value')::boolean "
            f"FROM {relation} AS {quote(inner)} WHERE " + " AND ".join(tests) + ")",
            read="postgres",
        )
        for table in replacement.find_all(exp.Table):
            table.meta["native_relation"] = True
        replacement.meta["semantic_binding"] = (dataset["id"], alias, spec.key)
        function.replace(replacement)


def execute_native(
    service,
    tenant,
    sql,
    tree,
    bindings,
    datasets,
    target,
    *,
    request,
    plan,
    max_evaluations,
    accept,
    reject,
    started,
    progress,
):
    if service.db.engine.dialect.name != "postgresql":
        raise ValueError(
            "Native semantic execution requires PostgreSQL and the jev_native extension"
        )
    if target is not None:
        raise ValueError(
            "Native mutation review is not available yet; use the python semantic engine"
        )
    operators = service.semantic_operators(tenant, tree, bindings)
    if any(spec.feature_id for _, _, _, spec, _ in operators):
        raise ValueError(
            "Maintained feature reviews require the python semantic engine until native review integration is available"
        )
    _validate_semantic_lineage(operators)
    groups = list(_source_groups(operators, bindings))
    if len(groups) > 32:
        raise ValueError("At most 32 independent source populations per native query")
    partial_allowed = _allows_partial_results(tree, operators)
    max_rows = int(os.getenv("SDD_NATIVE_MAX_ROWS", "100000"))
    timeout_ms = int(os.getenv("SDD_NATIVE_TIMEOUT_MS", "120000"))
    if not 1 <= max_rows <= 1_000_000 or not 1 <= timeout_ms <= 600_000:
        raise ValueError("Invalid native source or statement limits")
    totals, details, steps, relations, evaluators = Counter(), [], [], {}, []
    with (
        NativeAdmission.reserve(service.db, tenant, max_evaluations) as admission,
        service.db.transaction(tenant, isolation_level="REPEATABLE READ") as connection,
    ):
        service.configure_transaction(connection)
        connection.execute(
            text("SELECT set_config('statement_timeout',:timeout,true)"),
            {"timeout": str(timeout_ms)},
        )
        version = connection.execute(
            text("SELECT extversion FROM pg_extension WHERE extname='jev_native'")
        ).scalar_one_or_none()
        if version is None:
            raise ValueError(
                "Install the jev_native PostgreSQL extension before enabling native execution"
            )
        snapshot = connection.execute(text("SELECT pg_current_snapshot()::text")).scalar_one()
        sources, source_specs = [], []
        for index, (dataset, query, source, group) in enumerate(groups):
            if progress is not None and not progress():
                raise ValueError("Query cancelled before native semantic dispatch")
            specs = {item[3].key: item[3] for item in group}
            if len(specs) > 32:
                raise ValueError("At most 32 independent semantic questions per source population")
            questions = {}
            for key, spec in specs.items():
                batch, _ = spec.questions({}, key)
                questions.update(batch)
                questions[key]["subject_column"] = spec.column
            source_sql = _source_text(service, connection, dataset, query, source)
            source_id = "s" + str(index)
            sources.append({"id": source_id, "sql": source_sql, "questions": questions})
            source_specs.append((source_id, dataset, specs))
        name = "_jev_" + uid()
        relation = 'pg_temp."' + name + '"'
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
        statement = f'CREATE TEMP TABLE "{name}" ON COMMIT DROP AS SELECT * FROM jev_native.scan_many(CAST(:sources AS jsonb),CAST(:options AS jsonb))'
        parameters = {
            "sources": json.dumps(sources, ensure_ascii=False),
            "options": json.dumps(options),
        }
        if progress is not None and not progress():
            raise ValueError("Query cancelled before native semantic dispatch")
        admission.inflight = True
        connection.execute(text(statement), parameters)
        usage = _usage(connection, relation)
        admission.requests = usage["requests"]
        admission.inflight = False
        totals.update(usage)
        steps.append({"sql": statement, "parameters": parameters})
        for source_id, dataset, specs in source_specs:
            coverage = _coverage(connection, relation, source_id)
            for key, spec in specs.items():
                stats = coverage.get(
                    key,
                    {"total": 0, "resolved": 0, "unknown": 0, "not_evaluated": 0, "unresolved": 0},
                )
                totals.update(stats)
                details.append(
                    {
                        "dataset_id": dataset["id"],
                        "column": spec.column,
                        "definition": spec.definition,
                        "kind": "noul",
                        **stats,
                    }
                )
            keys = ",".join(
                "(source -> " + exp.Literal.string(key).sql(dialect="postgres") + ")"
                for key in dataset["primary_key"]
            )
            source_literal = exp.Literal.string(source_id).sql(dialect="postgres")
            connection.execute(
                text(f"CREATE UNIQUE INDEX ON {relation} ({keys}) WHERE source_id={source_literal}")
            )
            for key in specs:
                relations[dataset["id"], key] = (relation, source_id)
        connection.execute(text(f"ANALYZE {relation}"))
        for row in connection.execute(
            text(
                f"SELECT DISTINCT observation->'evaluator' AS evaluator FROM {relation} WHERE observation IS NOT NULL"
            )
        ):
            evaluators.append(row[0])
        receipts = [
            row[0]
            for row in connection.execute(
                text(f"SELECT DISTINCT receipt FROM {relation} WHERE receipt IS NOT NULL")
            )
        ]
        complete = not totals["unresolved"]
        if not complete and not partial_allowed:
            raise ValueError(
                "Unresolved native decisions require a simple partial read with direct semantic projections and NULL-preserving predicates; inspect decisions or increase the evaluation budget"
            )
        _replace(operators, relations)
        compiled, parameters = service.bind(tree, bindings)
        result = connection.execute(
            text("SELECT * FROM (" + compiled + ") AS _sdd_result LIMIT 1001"), parameters
        )
        if len(result.keys()) != len(set(result.keys())):
            raise ValueError("Duplicate output names require distinct SQL aliases")
        rows = [serial(dict(row)) for row in result.mappings()]
        manifest = {
            "execution_backend": "rust_postgresql",
            "admission_id": admission.identity,
            "native_version": version,
            "native_scheduler": "shared_round_robin",
            "native_source_populations": len(sources),
            "dataset_ids": [dataset["id"] for dataset in datasets],
            "source_snapshot": snapshot,
            "snapshot_mode": "postgres_repeatable_read",
            "source_rows": None,
            "source_rows_state": "NOT_EVALUATED",
            "semantic_source_rows": totals["source_rows"],
            "semantic_snapshot": None,
            "semantic_snapshot_state": "NOT_EVALUATED",
            "semantic_coverage": dict(totals),
            "evidence": details,
            "evidence_retention": "native_registry"
            if totals["stored_observations"] or totals["durable_reused_rows"]
            else "coverage_summary",
            "evidence_receipts": receipts,
            "evaluator": evaluators,
            "decision_policy": {"accept": accept, "reject": reject},
            "complete": complete,
            "result_is_partial": not complete,
            "truncated": len(rows) > 1000,
            "operation": type(tree).__name__.lower(),
            "planning_ms": (plan or {}).get("planning_ms", 0),
            "execution_ms": round((time.perf_counter() - started) * 1000, 2),
            "execution_steps": steps + [{"sql": compiled, "parameters": serial(parameters)}],
        }
        return service.save(
            connection, tenant, request, sql, compiled, parameters, plan, manifest, rows[:1000]
        )
