"""Lower executable typed stages into the native PostgreSQL plan contract."""

from copy import deepcopy
from dataclasses import asdict


def native_plan(dag, target, *, questions=None, requirements=None, guards=None):
    """Attach semantic questions; default consumers require all parent decisions.

    requirements maps (consumer, parent) to required question IDs. An explicit
    empty list permits unresolved decisions to remain in the input relation.
    guards maps consumer IDs to {input, question, equals} scalar conditions.
    """
    questions = questions or {}
    requirements = requirements or {}
    guards = guards or {}
    identities = [identity for layer in dag.layers(target) for identity in layer]
    if (set(questions) | set(guards)) - set(identities):
        raise ValueError("Semantic declarations must belong to the target graph")
    edges = {(identity, parent) for identity in identities for parent in dag.nodes[identity].inputs}
    if set(requirements) - edges:
        raise ValueError("Completeness requirements must name a graph dependency")
    stages = []
    for identity in identities:
        node = dag.nodes[identity]
        inputs = []
        for parent in node.inputs:
            required = list(requirements.get((identity, parent), questions.get(parent, {})))
            if set(required) - set(questions.get(parent, {})):
                raise ValueError("Completeness requirement names an undeclared decision")
            inputs.append({"stage": parent, "alias": parent, "require_values": required})
        stage = {
            "id": identity,
            "operator": "semantic" if identity in questions else node.operation,
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
        stages.append(stage)
    return {"version": 1, "target": target, "stages": stages}
