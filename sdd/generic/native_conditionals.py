"""Lower conditional SQL into shared row identities, guarded work and obligations."""

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from .semantic_types import SemanticSpec


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def literal(value):
    return "'" + value.replace("'", "''") + "'"


def known(value):
    return f"jsonb_build_object('output_state','VALUE','operation_state','SUCCEEDED','value',({value}),'raw','{{}}'::jsonb)"


def blocked(state):
    return f"jsonb_build_object('output_state','NOT_EVALUATED','operation_state','{state}','reason','Conditional input is unavailable')"


@dataclass(frozen=True)
class DecisionRef:
    stage: str
    column: str
    question: str | None = None

    def sql(self):
        result = f"{quote(self.stage)}.{quote(self.column)}"
        return f"({result}->{literal(self.question)})" if self.question else result

    def value(self):
        return f"({self.sql()}->>'value')::boolean"

    def ready(self):
        return f"({self.sql()}->>'output_state'='VALUE' AND {self.sql()}->>'operation_state'='SUCCEEDED')"

    def is_value(self, value):
        return f"({self.ready()} AND {self.value()} IS {'TRUE' if value else 'FALSE'})"


def required(mask, decision):
    if mask is None:
        return decision.ready()
    return f"({mask.is_value(False)} OR ({mask.is_value(True)} AND {decision.ready()}))"


def semantic_expression(node):
    return any(function.name.upper() == "SEMANTIC" for function in node.find_all(exp.Anonymous))


class ConditionalScope:
    def __init__(self, add, population, inputs, metadata):
        self.add = add
        self.metadata = metadata
        self.row = "_case_row"
        while self.row in metadata:
            self.row = "_" + self.row
        population = population.copy()
        population.select(
            exp.alias_(exp.Window(this=exp.RowNumber()), self.row, quoted=True),
            append=True,
            copy=False,
        )
        self.root = add(
            population,
            inputs,
            columns={
                **metadata,
                self.row: {"kind": "integer", "label": "Internal row identity", "nullable": False},
            },
            keys=[[self.row]],
        )
        self.semantic_groups, self.boolean_cache, self.route_cache = {}, {}, {}

    def query(self, projections, references=()):
        parents = [
            self.root["id"],
            *sorted({ref.stage for ref in references if ref} - {self.root["id"]}),
        ]
        sql = "SELECT " + ",".join(projections) + " FROM " + quote(parents[0])
        for parent in parents[1:]:
            sql += f" LEFT JOIN {quote(parent)} ON {quote(parent)}.{quote(self.row)}={quote(parents[0])}.{quote(self.row)}"
        tree = sqlglot.parse_one(sql, read="postgres")
        for table in tree.find_all(exp.Table):
            table.meta["native_relation"] = True
        return tree, parents

    def create(self, projections, columns, references=(), **contracts):
        identity = f"{quote(self.root['id'])}.{quote(self.row)}"
        query, parents = self.query([identity, *projections], references)
        stage = self.add(
            query,
            parents,
            columns={self.row: self.root["columns"][self.row], **columns},
            keys=[[self.row]],
            **contracts,
        )
        for source in stage["inputs"]:
            source["require_values"] = []
        return stage

    def derive(self, payload, references, mask):
        if mask is not None:
            payload = f"CASE WHEN {mask.is_value(False)} THEN {blocked('SKIPPED')} WHEN {mask.is_value(True)} THEN {payload} ELSE {blocked('BLOCKED_BY_DEPENDENCY')} END"
        stage = self.create(
            [f"{payload} AS _decision"],
            {"_decision": {"kind": "json", "label": "Conditional decision"}},
            [*references, mask],
        )
        return DecisionRef(stage["id"], "_decision")

    def semantic(self, function, mask):
        args = function.expressions
        if (
            len(args) != 2
            or not isinstance(args[0], exp.Column)
            or not isinstance(args[1], exp.Literal)
            or not args[1].is_string
        ):
            raise ValueError(
                "Conditional SEMANTIC requires a projected column and literal definition"
            )
        subject = args[0].name
        if args[0].table != self.root["id"] or subject not in self.metadata:
            raise ValueError("Conditional semantic subject is absent from its row population")
        spec = SemanticSpec(subject, args[1].this)
        questions, _ = spec.questions({}, spec.key)
        questions[spec.key]["subject_column"] = subject
        questions[spec.key]["instructions"]["context_columns"] = {
            name: column["label"] for name, column in self.metadata.items()
        }
        stage = self.semantic_groups.get(mask)
        if stage is None:
            projections = [f"{quote(self.root['id'])}.{quote(name)}" for name in self.metadata]
            columns = dict(self.metadata)
            guard = None
            if mask is not None:
                routing = "_case_route"
                while routing in columns or routing == self.row:
                    routing = "_" + routing
                projections.append(f"{mask.sql()} AS {quote(routing)}")
                columns[routing] = {"kind": "json", "label": "Conditional routing"}
                guard = {"column": routing, "equals": True}
            stage = self.create(
                projections,
                columns,
                [mask],
                questions=questions,
                context_columns=list(self.metadata),
                **({"row_guard": guard} if guard else {}),
            )
            self.semantic_groups[mask] = stage
        else:
            stage["questions"].update(questions)
        return DecisionRef(stage["id"], "__jev_decisions", spec.key)

    def boolean(self, node, mask):
        cache_key = node.sql(dialect="postgres"), mask
        if cache_key in self.boolean_cache:
            return self.boolean_cache[cache_key]
        if any(isinstance(item, (exp.AggFunc, exp.Window)) for item in node.walk()):
            raise ValueError(
                "Project aggregate or window conditions in a CTE before conditional SEMANTIC"
            )
        if not semantic_expression(node):
            result = self.derive(known(node.sql(dialect="postgres")), [], mask)
        elif isinstance(node, exp.Paren):
            result = self.boolean(node.this, mask)
        elif isinstance(node, exp.Anonymous) and node.name.upper() == "SEMANTIC":
            result = self.semantic(node, mask)
        elif isinstance(node, (exp.And, exp.Or)):
            dominant = isinstance(node, exp.Or)
            operator = "OR" if dominant else "AND"
            operands = [node.this, node.expression]
            exact = next(
                (
                    index
                    for index, operand in enumerate(operands)
                    if not semantic_expression(operand)
                ),
                None,
            )
            if exact is None:
                left, right = [self.boolean(operand, mask) for operand in operands]
            else:
                fixed = self.boolean(operands[exact], mask)
                needed = f"CASE WHEN {fixed.ready()} THEN {known(fixed.value() + (' IS NOT TRUE' if dominant else ' IS NOT FALSE'))} ELSE {blocked('BLOCKED_BY_DEPENDENCY')} END"
                if mask is not None:
                    needed = (
                        f"CASE WHEN {mask.is_value(False)} THEN {known('false')} ELSE {needed} END"
                    )
                eligibility = self.derive(needed, [fixed, mask], None)
                dependent = self.boolean(operands[1 - exact], eligibility)
                left, right = (fixed, dependent) if exact == 0 else (dependent, fixed)
            payload = f"CASE WHEN {left.is_value(dominant)} OR {right.is_value(dominant)} THEN {known(str(dominant).lower())} WHEN {left.ready()} AND {right.ready()} THEN {known(f'{left.value()} {operator} {right.value()}')} ELSE {blocked('BLOCKED_BY_DEPENDENCY')} END"
            result = self.derive(payload, [left, right], mask)
        elif isinstance(node, exp.Not):
            child = self.boolean(node.this, mask)
            result = self.derive(
                f"CASE WHEN {child.ready()} THEN {known('NOT ' + child.value())} ELSE {blocked('BLOCKED_BY_DEPENDENCY')} END",
                [child],
                mask,
            )
        else:
            value, refs, obligations = self.expression(node, mask)
            payload = known(value.sql(dialect="postgres"))
            if obligations:
                ready = " AND ".join(required(*item) for item in obligations)
                payload = (
                    f"CASE WHEN {ready} THEN {payload} ELSE {blocked('BLOCKED_BY_DEPENDENCY')} END"
                )
            result = self.derive(payload, refs, mask)
        self.boolean_cache[cache_key] = result
        return result

    def routes(self, condition, mask):
        key = condition, mask
        if key in self.route_cache:
            return self.route_cache[key]
        projections = []
        for name, test in (("_matched", "TRUE"), ("_remaining", "NOT TRUE")):
            payload = f"CASE WHEN {condition.ready()} THEN {known(condition.value() + ' IS ' + test)} ELSE {blocked('BLOCKED_BY_DEPENDENCY')} END"
            if mask is not None:
                payload = f"CASE WHEN {mask.is_value(False)} THEN {known('false')} WHEN {mask.is_value(True)} THEN {payload} ELSE {blocked('BLOCKED_BY_DEPENDENCY')} END"
            projections.append(f"{payload} AS {name}")
        stage = self.create(
            projections,
            {
                name: {"kind": "json", "label": "Branch eligibility"}
                for name in ("_matched", "_remaining")
            },
            [condition, mask],
        )
        result = DecisionRef(stage["id"], "_matched"), DecisionRef(stage["id"], "_remaining")
        self.route_cache[key] = result
        return result

    def expression(self, node, mask=None):
        if isinstance(node, exp.Anonymous) and node.name.upper() == "SEMANTIC":
            ref = self.semantic(node, mask)
            return sqlglot.parse_one(ref.value(), read="postgres"), {ref}, [(mask, ref)]
        if isinstance(node, exp.Case) and any(
            f.name.upper() == "SEMANTIC" for f in node.find_all(exp.Anonymous)
        ):
            result, refs, obligations, active = node.copy(), set(), [], mask
            branches = []
            for branch in node.args.get("ifs", []):
                condition = (
                    branch.this
                    if node.this is None
                    else exp.EQ(this=node.this.copy(), expression=branch.this.copy())
                )
                decision = self.boolean(condition, active)
                selected, remaining = self.routes(decision, active)
                refs.update([decision, selected, remaining])
                if active:
                    refs.add(active)
                obligations.append((active, decision))
                value, dependencies, needed = self.expression(branch.args["true"], selected)
                branches.append(
                    exp.If(this=sqlglot.parse_one(decision.value(), read="postgres"), true=value)
                )
                refs.update(dependencies)
                obligations.extend(needed)
                active = remaining
            result.set("this", None)
            result.set("ifs", branches)
            if node.args.get("default") is not None:
                value, dependencies, needed = self.expression(node.args["default"], active)
                result.set("default", value)
                refs.update(dependencies)
                obligations.extend(needed)
            return result, refs, obligations
        result, refs, obligations = node.copy(), set(), []
        for key, value in node.args.items():
            items = value if isinstance(value, list) else [value]
            rewritten = []
            for item in items:
                if isinstance(item, exp.Expression):
                    child, dependencies, needed = self.expression(item, mask)
                    rewritten.append(child)
                    refs.update(dependencies)
                    obligations.extend(needed)
                else:
                    rewritten.append(item)
            result.set(key, rewritten if isinstance(value, list) else rewritten[0])
        return result, refs, obligations

    def finish(self, query, names):
        query = query.copy()
        for column in list(query.find_all(exp.Column)):
            name = names.get((column.table, column.name))
            if name is not None:
                column.replace(exp.column(name, table=self.root["id"], quoted=True))
        from .native_relational import _table

        query.set("from_", exp.From(this=_table(self.root["id"])))
        query.set("joins", None)
        query, refs, obligations = self.expression(query)
        obligations = list(dict.fromkeys(obligations))
        refs.update(ref for pair in obligations for ref in pair if ref)
        columns, projections = dict(self.root["columns"]), []
        projections.extend(f"{quote(self.root['id'])}.{quote(name)}" for name in self.metadata)

        def field(payload):
            name = f"_case_value_{len(columns)}"
            while name in columns:
                name = "_" + name
            columns[name] = {"kind": "json", "label": "Conditional evidence"}
            projections.append(f"{payload} AS {quote(name)}")
            return name

        storage = {
            pair: field(f"{quote(pair[0])}.{quote(pair[1])}")
            for pair in sorted({(ref.stage, ref.column) for ref in refs})
        }
        individual = {
            ref: field(ref.sql()) if ref.question else storage[ref.stage, ref.column]
            for ref in sorted(refs, key=lambda item: (item.stage, item.column, item.question or ""))
        }
        satisfied = field(known("true"))
        selections = {
            f"required_{index}": {
                "selector": individual[mask] if mask else satisfied,
                "cases": [
                    {"equals": True, "column": individual[decision]},
                    {"equals": False, "column": satisfied},
                ],
            }
            for index, (mask, decision) in enumerate(obligations)
        }
        stage = self.create(
            projections,
            {name: metadata for name, metadata in columns.items() if name != self.row},
            sorted(refs, key=lambda item: (item.stage, item.column, item.question or "")),
            selections=selections,
        )
        for column in list(query.find_all(exp.Column)):
            if column.table == self.root["id"]:
                column.set("table", exp.to_identifier(stage["id"], quoted=True))
            elif (column.table, column.name) in storage:
                column.replace(
                    exp.column(storage[column.table, column.name], table=stage["id"], quoted=True)
                )
        query.set("from_", exp.From(this=_table(stage["id"])))
        return self.add(query, [stage["id"]])
