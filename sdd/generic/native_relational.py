"""Compile semantic decisions over derived SQL relations into a shared native DAG."""

from collections import Counter

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

from .native_sql import _conjuncts
from .semantic_types import SemanticSpec


def needs_relational_plan(tree):
    for scope in traverse_scope(tree):
        for function in scope.expression.find_all(exp.Anonymous):
            if (
                function.name.upper() != "SEMANTIC"
                or function.find_ancestor(exp.Select) is not scope.expression
            ):
                continue
            if function.expressions and isinstance(function.expressions[0], exp.Column):
                if isinstance(scope.sources.get(function.expressions[0].table), Scope):
                    return True
    return False


def _table(identity, alias=None):
    node = exp.table_(identity, quoted=True)
    if alias:
        node = node.as_(alias, quoted=True)
    node.meta["native_relation"] = True
    return node


def _columns(query):
    names = query.named_selects
    if not names or len(names) != len(set(names)) or "*" in names:
        raise ValueError("Native stage outputs require distinct, explicit SQL names")
    return {name: {"kind": "other", "label": name, "nullable": True} for name in names}


def compile_relational_plan(tree, bindings, render):
    """Lower an already authorized and qualified query; render binds its SQL literals."""
    if any(f.name.upper() == "SEMANTIC_FEATURE" for f in tree.find_all(exp.Anonymous)):
        raise ValueError("Maintained features are not available in dependent native plans")
    if any(node.args.get("recursive") for node in tree.find_all(exp.With)):
        raise ValueError("Recursive semantic populations require a bounded stage contract")
    stages, completed = [], {}

    def add(query, inputs, *, columns=None, questions=None):
        if len(stages) >= 32:
            raise ValueError("At most 32 stages per dependent native query")
        identity = f"native_stage_{len(stages)}"
        stage = {
            "id": identity,
            "operator": "semantic" if questions else "source",
            "sql": render(query),
            "columns": columns or _columns(query),
            "inputs": [{"stage": parent, "alias": parent} for parent in dict.fromkeys(inputs)],
        }
        if questions:
            stage["questions"] = questions
        stages.append(stage)
        return stage

    for scope in traverse_scope(tree):
        if scope.is_correlated_subquery or scope.subquery_scopes:
            raise ValueError(
                "Dependent semantic SQL currently requires uncorrelated relation stages"
            )
        query = scope.expression.copy()
        local_scope = traverse_scope(query)[-1]
        inputs, fields = [], []
        if isinstance(query, exp.SetOperation):
            for key, child in zip(("this", "expression"), scope.union_scopes):
                parent = completed[id(child)]
                inputs.append(parent["id"])
                query.set(key, exp.select("*").from_(_table(parent["id"])))
            query.set("with_", None)
            columns = completed[id(scope.union_scopes[0])]["columns"]
            completed[id(scope)] = add(query, inputs, columns=columns)
            continue
        if not isinstance(query, exp.Select):
            raise ValueError("Unsupported native relation stage")
        for alias, (_, source) in scope.selected_sources.items():
            if isinstance(source, Scope):
                parent = completed[id(source)]
                inputs.append(parent["id"])
                node = local_scope.selected_sources[alias][0]
                if isinstance(node, exp.Query):
                    node = node.parent
                node.replace(_table(parent["id"], alias))
                names = parent["columns"]
            else:
                dataset = bindings[id(source)]
                names = {
                    column["name"]: {
                        "kind": column.get("native_kind", column["type"]),
                        "label": column["name"],
                        "nullable": True,
                    }
                    for column in dataset["columns"]
                }
                for key in dataset["primary_key"]:
                    names.setdefault(key, {"kind": "other", "label": key, "nullable": False})
            fields.extend((alias, name, metadata) for name, metadata in names.items())
        query.set("with_", None)
        functions = [f for f in query.find_all(exp.Anonymous) if f.name.upper() == "SEMANTIC"]
        if not functions:
            completed[id(scope)] = add(query, inputs)
            continue

        occurrences = Counter(name for _, name, _ in fields)
        names, metadata, projections = {}, {}, []
        for index, (alias, name, kind) in enumerate(fields):
            output = (
                name
                if occurrences[name] == 1 and not name.startswith("__jev_")
                else f"_field_{index}"
            )
            while output in metadata:
                output = "_" + output
            names[alias, name] = output
            metadata[output] = {**kind, "label": f"{alias}.{name}"}
            projections.append(
                exp.alias_(exp.column(name, table=alias, quoted=True), output, quoted=True)
            )
        if not projections:
            raise ValueError("Semantic stage requires a source relation")
        population = exp.select(*projections)
        population.set("from_", query.args["from_"].copy())
        population.set("joins", [join.copy() for join in query.args.get("joins", [])])
        if query.args.get("where"):
            exact = [
                term.copy()
                for term in _conjuncts(query.args["where"].this)
                if not any(f.name.upper() == "SEMANTIC" for f in term.find_all(exp.Anonymous))
            ]
            if exact:
                population = population.where(exp.and_(*exact))
        if any(f.name.upper() == "SEMANTIC" for f in population.find_all(exp.Anonymous)):
            raise ValueError(
                "Dependent semantic join conditions need an explicit projected relation"
            )
        questions, replacements = {}, []
        for function in functions:
            args = function.expressions
            if (
                len(args) != 2
                or not isinstance(args[0], exp.Column)
                or not isinstance(args[1], exp.Literal)
                or not args[1].is_string
            ):
                raise ValueError(
                    "SEMANTIC requires a projected column and a literal definition; put calculations in a CTE"
                )
            subject = names.get((args[0].table, args[0].name))
            if subject is None:
                raise ValueError("Semantic subject is absent from the input relation")
            spec = SemanticSpec(subject, args[1].this)
            question, _ = spec.questions({}, spec.key)
            question[spec.key]["subject_column"] = subject
            question[spec.key]["instructions"]["context_columns"] = {
                name: column["label"] for name, column in metadata.items()
            }
            questions.update(question)
            replacements.append((function, spec.key))
        evaluated = add(population, inputs, columns=metadata, questions=questions)
        identity = evaluated["id"]
        for function, key in replacements:
            function.replace(
                sqlglot.parse_one(
                    f"(\"{identity}\".__jev_decisions -> '{key}' ->> 'value')::boolean",
                    read="postgres",
                )
            )
        for column in list(query.find_all(exp.Column)):
            if column.table == identity and column.name == "__jev_decisions":
                continue
            output = names.get((column.table, column.name))
            if output is not None:
                column.replace(exp.column(output, table=identity, quoted=True))
        query.set("from_", exp.From(this=_table(identity)))
        query.set("joins", None)
        completed[id(scope)] = add(query, [identity])

    target = stages[-1]["id"]
    reachable, pending = set(), [target]
    by_id = {stage["id"]: stage for stage in stages}
    while pending:
        identity = pending.pop()
        if identity not in reachable:
            reachable.add(identity)
            pending.extend(item["stage"] for item in by_id[identity]["inputs"])
    return {
        "version": 1,
        "target": target,
        "stages": [stage for stage in stages if stage["id"] in reachable],
    }
