"""Lower executable typed stages into the native PostgreSQL plan contract."""

from copy import deepcopy
from dataclasses import asdict


def native_plan(
    dag,
    target,
    *,
    questions=None,
    requirements=None,
    guards=None,
    row_guards=None,
    selections=None,
    contexts=None,
):
    """Attach semantic questions; default consumers require all parent decisions.

    requirements maps (consumer, parent) to required question IDs. An explicit
    empty list permits unresolved decisions to remain in the input relation.
    guards maps consumer IDs to {input, question, equals} scalar conditions.
    row_guards maps semantic stages to {column, equals} decision conditions.
    selections maps merge stages to named, deterministic decision selections.
    contexts maps semantic stages to the projected columns sent to the provider.
    """
    questions = questions or {}
    requirements = requirements or {}
    guards = guards or {}
    row_guards = row_guards or {}
    selections = selections or {}
    contexts = contexts or {}
    if set(questions) & set(selections):
        raise ValueError("A stage either evaluates questions or merges decisions")
    identities = [identity for layer in dag.layers(target) for identity in layer]
    if (set(questions) | set(guards) | set(row_guards) | set(selections) | set(contexts)) - set(
        identities
    ):
        raise ValueError("Semantic declarations must belong to the target graph")
    edges = {(identity, parent) for identity in identities for parent in dag.nodes[identity].inputs}
    if set(requirements) - edges:
        raise ValueError("Completeness requirements must name a graph dependency")
    stages = []
    for identity in identities:
        node = dag.nodes[identity]
        inputs = []
        for parent in node.inputs:
            decisions = questions.get(parent, selections.get(parent, {}))
            required = list(requirements.get((identity, parent), decisions))
            if set(required) - set(decisions):
                raise ValueError("Completeness requirement names an undeclared decision")
            inputs.append({"stage": parent, "alias": parent, "require_values": required})
        stage = {
            "id": identity,
            "operator": "semantic"
            if identity in questions
            else "merge"
            if identity in selections
            else node.operation,
            "sql": node.query.sql(dialect="postgres"),
            "columns": {name: asdict(column) for name, column in node.columns.items()},
            "inputs": inputs,
            "grain": node.grain,
            "keys": node.keys,
            "assertions": list(node.assertions),
        }
        if identity in questions:
            if not questions[identity]:
                raise ValueError("A semantic stage needs at least one question")
            stage["questions"] = deepcopy(questions[identity])
        if identity in guards:
            guard = guards[identity]
            if guard.get("input") not in node.inputs:
                raise ValueError("A guard must refer to a direct graph dependency")
            stage["guard"] = deepcopy(guard)
        if identity in row_guards:
            guard = row_guards[identity]
            column = node.columns.get(guard.get("column"))
            if identity not in questions or column is None or column.kind != "json":
                raise ValueError("A row guard needs a semantic stage and a JSON decision column")
            stage["row_guard"] = deepcopy(guard)
        if identity in selections:
            if not selections[identity]:
                raise ValueError("A merge stage needs at least one selection")
            stage["selections"] = deepcopy(selections[identity])
        if identity in contexts:
            columns = contexts[identity]
            subjects = {
                q["subject_column"]
                for q in questions.get(identity, {}).values()
                if q.get("subject_column")
            }
            if (
                identity not in questions
                or not columns
                or len(set(columns)) != len(columns)
                or set(columns) - set(node.columns)
                or subjects - set(columns)
                or row_guards.get(identity, {}).get("column") in columns
            ):
                raise ValueError(
                    "Semantic context must name distinct projected columns, include every subject and exclude routing metadata"
                )
            stage["context_columns"] = list(columns)
        stages.append(stage)
    return {"version": 1, "target": target, "stages": stages}
