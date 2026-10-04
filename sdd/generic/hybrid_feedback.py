"""Observed values, backend checks and concrete feedback for one bounded repair."""

from copy import deepcopy
import os

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlglot import exp, parse_one
from sqlglot.optimizer.scope import traverse_scope
from sqlglot.lineage import lineage
from sqlglot.errors import SqlglotError

from .sql import FUNCTIONS


def backend_contract(catalog):
    sqlite = catalog.db.engine.dialect.name == "sqlite"
    engine = os.getenv("SDD_SEMANTIC_ENGINE", "python")
    semantic_rules = [
        "SEMANTIC(column, literal_definition) tests text meaning. Use exact SQL for known codes, categories, dates and quantities.",
        "Never use semantic evaluation to compensate for not inspecting a category's stored representation.",
    ]
    if engine == "native" and not sqlite:
        semantic_rules.extend(
            [
                "SEMANTIC can read a named column of an uncorrelated CTE or derived table. Put required calculations or text aggregation in that relation first, then judge its actual output. Project only the context needed for the judgment.",
                "Native dependent stages share one DAG and budget. Independent branches run concurrently; exact consumers wait for resolved input decisions. A held result is NOT_EVALUATED, never a zero count.",
                "CASE routes semantic work to each row's selected branch, including nested or simple CASE. A SQL NULL condition falls through; an unresolved semantic condition holds the result instead of choosing ELSE. Project aggregate or window conditions in a preceding CTE.",
                "Dependent semantic plans do not support recursive CTEs, correlated or scalar subqueries, SEMANTIC_FEATURE, or semantic writes. Base-column scans require registered primary keys; derived relations retain row multiplicity without requiring a key.",
            ]
        )
    else:
        semantic_rules.append(
            "SEMANTIC requires a base text column and a registered nonempty primary key."
        )
        if engine == "native":
            semantic_rules.append(
                "Native semantic execution requires PostgreSQL; it cannot run on SQLite."
            )
    return {
        "input_dialect": "PostgreSQL, restricted to the registered calculation catalog",
        "execution_backend": catalog.db.engine.dialect.name,
        "semantic_engine": engine,
        "semantic_rules": semantic_rules,
        "allowed_functions": sorted(FUNCTIONS),
        "construction_rules": [
            "Use only listed functions. AGE and regular-expression predicates are not registered.",
            "Use exact SQL comparisons for codes, named categories, dates and quantities. SEMANTIC is for meanings that cannot be represented by known values and explicit predicates.",
            "A value sample is not a closed vocabulary or a population filter. Do not infer absence from an omitted sample value.",
            *(
                [
                    "SQLite cannot execute LATERAL joins. Prefer windows or scalar correlated subqueries.",
                    "Prefer EXTRACT for ISO dates. Do not use PostgreSQL date/interval arithmetic or formatted TO_TIMESTAMP without an executable alternative.",
                ]
                if sqlite
                else []
            ),
        ],
    }


def observed_context(tenant, packet, catalog):
    """Read bounded records only after retrieval; samples never redefine query scope."""
    output = deepcopy(packet)
    output["backend_contract"] = backend_contract(catalog)
    trace = {
        "output_state": "VALUE",
        "operation_state": "SUCCEEDED",
        "tables": 0,
        "sample_only": True,
    }
    datasets = {d["name"]: d for d in catalog.list(tenant)}
    from .planning_samples import PlanningSamples

    sampler = PlanningSamples(catalog, row_limit=256)
    items = output["catalog"][:16]
    batches = sampler.many(
        tenant,
        [
            (
                datasets[item["name"]],
                [c["name"] for c in item["columns"] if not c.get("feature_id")][:24],
            )
            for item in items
        ],
    )
    omitted_columns = 0
    for index, item in enumerate(output["catalog"]):
        sample = batches[index] if index < len(batches) else None
        for column in item["columns"]:
            if column.get("feature_id"):
                continue
            if sample is None:
                column["value_evidence"] = {
                    "sample_only": True,
                    "examples": [],
                    "output_state": "NOT_EVALUATED",
                    "operation_state": "BLOCKED_BY_BUDGET",
                }
            else:
                column["value_evidence"] = sample.evidence(column["name"])
                column["value_evidence"].pop("exact_values", None)
                evidence = column["value_evidence"]
                if column["type"] == "text" and not evidence["complete"]:
                    lookup = sampler.matches(
                        tenant, datasets[item["name"]], column["name"], packet["request"]
                    )
                    evidence["examples"] = list(
                        dict.fromkeys([*lookup["values"], *evidence["examples"]])
                    )[:16]
                    evidence["literal_lookup"] = {k: v for k, v in lookup.items() if k != "values"}
            if column["value_evidence"]["output_state"] == "NOT_EVALUATED":
                omitted_columns += 1
        if sample and sample.output_state == "VALUE":
            trace["tables"] += 1
        else:
            trace.setdefault("unavailable", []).append(
                {
                    "table": item["name"],
                    "output_state": sample.output_state if sample else "NOT_EVALUATED",
                    "operation_state": sample.operation_state if sample else "BLOCKED_BY_BUDGET",
                }
            )
    trace.update(
        source_reads=sampler.reads, sample_bytes=sampler.bytes, omitted_columns=omitted_columns
    )
    if any(item["output_state"] == "UNKNOWN" for item in trace.get("unavailable", [])):
        trace.update(output_state="UNKNOWN", operation_state="FAILED")
    elif (
        trace.get("unavailable")
        or omitted_columns
        or any(not sample.complete or sample.clipped for sample in batches)
    ):
        trace.update(
            output_state="VALUE" if trace["tables"] else "NOT_EVALUATED",
            operation_state="TRUNCATED" if trace["tables"] else "BLOCKED_BY_BUDGET",
        )
    return output, trace


def checked_candidate(service, tenant, candidate, allowed):
    item = dict(candidate)
    try:
        tree, bindings, _, mutation = service.prepare(tenant, item["sql"])
        if any(d["id"] not in allowed for d in bindings.values()):
            raise ValueError("Query references an unselected dataset")
        semantic = any(
            n.name.upper() in ("SEMANTIC", "SEMANTIC_FEATURE") for n in tree.find_all(exp.Anonymous)
        )
        derived = False
        if semantic and service.semantic_engine == "native":
            from .native_relational import compile_relational_plan, needs_relational_plan
            from .native_sql import _validate_semantic_lineage

            if service.db.engine.dialect.name != "postgresql":
                raise ValueError("Native semantic execution requires PostgreSQL")
            if mutation is not None or any(
                node.name.upper() == "SEMANTIC_FEATURE" for node in tree.find_all(exp.Anonymous)
            ):
                raise ValueError("Native semantic writes and maintained features are not supported")
            derived = needs_relational_plan(tree)
            if derived:
                compile_relational_plan(tree, bindings, lambda query: query.sql(dialect="postgres"))
            else:
                _validate_semantic_lineage(service.semantic_operators(tenant, tree, bindings))
        elif semantic:
            service.semantic_operators(tenant, tree, bindings)
        unkeyed_semantic = any(
            not bindings[id(source)]["primary_key"]
            for scope in traverse_scope(tree)
            for node in scope.find_all(exp.Anonymous)
            if node.name.upper() in ("SEMANTIC", "SEMANTIC_FEATURE")
            and node.expressions
            and isinstance(node.expressions[0], exp.Column)
            for _, source in [scope.selected_sources.get(node.expressions[0].table, (None, None))]
            if isinstance(source, exp.Table) and id(source) in bindings
        )
        if unkeyed_semantic and not derived:
            raise ValueError(
                "Semantic row evaluation requires registered primary keys; use observed category values and ordinary SQL when the condition is categorical"
            )
        item.update(valid=True, operation=tree.key if mutation is not None else "select")
        if mutation is not None or semantic:
            item["backend_probe"] = {"output_state": "NOT_EVALUATED", "operation_state": "SKIPPED"}
        else:
            compiled, parameters = service.bind(tree, bindings)
            with service.db.transaction(tenant) as connection:
                service.configure_transaction(connection)
                prefix = (
                    "EXPLAIN QUERY PLAN "
                    if service.db.engine.dialect.name == "sqlite"
                    else "EXPLAIN (FORMAT JSON) "
                )
                connection.execute(text(prefix + compiled), parameters).all()
            item["backend_probe"] = {"output_state": "VALUE", "operation_state": "SUCCEEDED"}
        item["projections"] = (
            [p.sql(dialect="postgres") for p in tree.selects] if mutation is None else []
        )
    except (ValueError, SqlglotError, DBAPIError) as exc:
        item.update(
            valid=False,
            error=str(exc).split("[SQL:")[0][:400],
            backend_probe={"output_state": "NOT_EVALUATED", "operation_state": "FAILED"},
        )
    return item


def repair_feedback(plan):
    trace = plan["hybrid"]
    candidates = trace.get("candidates", [])
    selected = next((c for c in candidates if c["id"] == trace.get("selected")), None)
    if selected is None:
        return [
            {
                "kind": "executable_sql",
                "candidate": c["id"],
                "problem": c.get("error", "No executable selection"),
            }
            for c in candidates
        ]
    # A probability alone is not an actionable editing instruction.
    feedback = list(trace.get("repair_tickets", []))
    if feedback:
        feedback.append(
            {
                "kind": "additional_context",
                "items": trace.get("plan_inspection", {})
                .get("additional_data", {})
                .get("helpful", []),
            }
        )
    return feedback


def quality(plan):
    trace = plan["hybrid"]
    checks = [
        v
        for k, v in trace.get("checks", {}).items()
        if k.startswith("check_" + trace.get("selected", "!") + "_")
    ]
    return (
        bool(plan.get("logical_sql")),
        min(checks, default=0),
        (trace.get("overall_appropriateness", {}).get("probability") or 0),
    )


def output_review_jobs(packet, candidates, noul, choice):
    """Review answer fields in parallel with SQL checks, without plan self-justification."""
    jobs = []
    for candidate in candidates:
        if not candidate.get("projections"):
            continue
        expressions = {}
        try:
            nodes = lineage(None, parse_one(candidate["sql"], read="postgres"), dialect="postgres")
            expressions = {
                name: [node.expression.sql(dialect="postgres") for node in root.walk()][:24]
                for name, root in nodes.items()
            }
        except (SqlglotError, ValueError):
            pass
        jobs.append(
            (
                {
                    "request": packet["request"],
                    "output_expressions": dict(enumerate(candidate["projections"])),
                    "expression_definitions": expressions,
                    "candidates": [
                        {"id": candidate["id"], "projections": candidate["projections"]}
                    ],
                },
                {
                    **{
                        f"check_role_{candidate['id']}_output{i}": choice(
                            f"What role does output expression {i} play in the ORIGINAL request? Classify the requested answer, not whether this field is useful to SQL. A ranking measure is not implicitly requested by a who/which-entity question.",
                            {
                                "answer": "Requested answer field or grouping identifier",
                                "support": "Only an intermediate filter, ranking or explanatory helper",
                                "ambiguous": "Cannot determine the requested answer shape",
                            },
                        )
                        for i in range(len(candidate["projections"]))
                    },
                    **{
                        f"check_{candidate['id']}_output{i}": noul(
                            f"Is output expression {i} itself requested as an answer, rather than only used to identify, filter or rank the answer? "
                            "Infer the answer target from the original request. Explanatory metrics and supporting dates are not requested unless the user asks for them. "
                            "Preserve requested identifiers, measures and grouping labels."
                        )
                        for i in range(len(candidate["projections"]))
                    },
                },
            )
        )
    return jobs


def review_context(packet):
    """Keep structural evidence and short value examples; avoid repeated profiling payloads."""
    output = deepcopy(packet)
    contract = output.pop("backend_contract", {})
    if contract.get("semantic_rules"):
        output["semantic_rules"] = contract["semantic_rules"]
    for table in output.get("catalog", []):
        for column in table["columns"]:
            evidence = column.pop("value_evidence", None)
            if evidence:
                column["sample_values"] = evidence.get("examples", [])[:4]
                column["sample_missingness"] = {
                    k: evidence[k]
                    for k in ("rows_inspected", "observed_nulls", "observed_blanks")
                    if k in evidence
                }
    return output


def projection_repair(feedback):
    relevant = [f for f in feedback if f["kind"] != "additional_context"]
    return (
        bool(relevant)
        and all(
            f["kind"] == "output" or (f["kind"].startswith("output") and f["kind"][6:].isdigit())
            for f in relevant
        )
        and not any(f.get("items") for f in feedback)
    )


def preserves_repair_scope(original, revised):
    """A projection-only repair cannot change relational clauses or shared CTEs."""
    try:
        trees = [parse_one(sql, read="postgres") for sql in (original, revised)]
        if not all(isinstance(tree, exp.Select) for tree in trees):
            return original == revised
        for tree in trees:
            tree.set("expressions", [])
            for node in tree.walk():
                node.comments = None
        return trees[0] == trees[1]
    except SqlglotError:
        return False
