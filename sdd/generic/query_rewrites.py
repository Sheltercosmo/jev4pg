"""Conservative, domain-independent relational rewrites using SQLGlot."""

from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope
from sqlglot.optimizer.unnest_subqueries import decorrelate


def decorrelate_scalar_aggregate(tree):
    subqueries = list(tree.find_all(exp.Subquery))
    if len(subqueries) != 1 or any(
        node.name.upper() in ("SEMANTIC", "SEMANTIC_FEATURE")
        for node in tree.find_all(exp.Anonymous)
    ):
        return tree
    query = subqueries[0].this
    if not isinstance(query, exp.Select) or len(query.expressions) != 1:
        return tree
    if any(
        query.args.get(key)
        for key in (
            "group",
            "having",
            "order",
            "limit",
            "offset",
            "qualify",
            "joins",
            "with_",
            "distinct",
        )
    ):
        return tree
    aggregates = list(query.expressions[0].find_all(exp.AggFunc))
    if len(aggregates) != 1 or not isinstance(aggregates[0], (exp.Avg, exp.Sum, exp.Min, exp.Max)):
        return tree
    allowed = (
        exp.Alias,
        exp.Avg,
        exp.Sum,
        exp.Min,
        exp.Max,
        exp.Column,
        exp.Identifier,
        exp.Literal,
        exp.Add,
        exp.Sub,
        exp.Mul,
        exp.Div,
        exp.Paren,
        exp.Neg,
    )
    if any(not isinstance(node, allowed) for node in query.expressions[0].walk()):
        return tree
    scope = next((scope for scope in traverse_scope(tree) if scope.expression is query), None)
    if not scope or not scope.external_columns or not query.args.get("where"):
        return tree
    inner_aliases = set(scope.sources)
    predicates = (
        list(query.args["where"].this.flatten())
        if isinstance(query.args["where"].this, exp.And)
        else [query.args["where"].this]
    )
    for predicate in predicates:
        if not isinstance(predicate, exp.EQ) or not all(
            isinstance(node, exp.Column) for node in (predicate.this, predicate.expression)
        ):
            return tree
        if (predicate.this.table in inner_aliases) == (predicate.expression.table in inner_aliases):
            return tree
    used_names = {node.name for node in tree.find_all(exp.Identifier)}
    alias_index = 0

    def next_alias():
        nonlocal alias_index
        while True:
            name = f"_sdd_aggregate_{alias_index}"
            alias_index += 1
            if name not in used_names:
                used_names.add(name)
                return name

    # Only this scope passed the proof above; sibling subqueries retain their SQL semantics.
    decorrelate(query, query.parent_select, scope.external_columns, next_alias)
    return tree
