"""Review stored remote expressions, independently of transport and source admission."""

from collections.abc import Callable

from sdd.operators.types import Decision, OperationState

from .pg_nodes import Node, NodeLimitError, TypedList, read_tree


# PostgreSQL 17 core scalar types; domains, composites and object references need separate review.
SCALAR_TYPES = {
    16,
    17,
    18,
    19,
    20,
    21,
    23,
    25,
    700,
    701,
    1042,
    1043,
    1082,
    1083,
    1114,
    1184,
    1186,
    1266,
    1700,
    2950,
}
FUNCTIONS = set(
    """
abs ceil ceiling floor round trunc mod power sqrt exp ln log sign
lower upper length char_length character_length octet_length bit_length
substring substr left right replace reverse btrim ltrim rtrim strpos split_part
date_part date_trunc age timezone to_char to_date to_timestamp
int2 int4 int8 float4 float8 numeric text bpchar varchar date time timestamp timestamptz interval
now transaction_timestamp statement_timestamp
count sum avg min max bool_and bool_or every stddev stddev_pop stddev_samp variance var_pop var_samp
row_number rank dense_rank percent_rank cume_dist ntile lag lead first_value last_value nth_value
""".split()
)
OPERATORS = set("= <> < <= > >= + - * / % ^ || ~~ !~~ ~~* !~~*".split())

# Unknown fields or nodes remain unproven, including additions in a future server version.
FIELDS = {
    "QUERY": (
        "commandType querySource canSetTag utilityStmt resultRelation hasAggs hasWindowFuncs "
        "hasTargetSRFs hasSubLinks hasDistinctOn hasRecursive hasModifyingCTE hasForUpdate "
        "hasRowSecurity isReturn cteList rtable rteperminfos jointree mergeActionList "
        "mergeTargetRelation mergeJoinCondition targetList override onConflict returningList "
        "groupClause groupDistinct groupingSets havingQual windowClause distinctClause "
        "sortClause limitOffset limitCount limitOption rowMarks setOperations constraintDeps "
        "withCheckOptions stmt_location stmt_len"
    ),
    "RANGETBLENTRY": (
        "alias eref rtekind relid inh relkind rellockmode perminfoindex tablesample subquery "
        "security_barrier jointype joinmergedcols joinaliasvars joinleftcols joinrightcols "
        "join_using_alias values_lists ctename ctelevelsup self_reference coltypes coltypmods "
        "colcollations lateral inFromCl securityQuals"
    ),
    "RTEPERMISSIONINFO": "relid inh requiredPerms checkAsUser selectedCols insertedCols updatedCols",
    "ALIAS": "aliasname colnames",
    "FROMEXPR": "fromlist quals",
    "RANGETBLREF": "rtindex",
    "JOINEXPR": "jointype isNatural larg rarg usingClause join_using_alias quals alias rtindex",
    "TARGETENTRY": "expr resno resname ressortgroupref resorigtbl resorigcol resjunk",
    "VAR": (
        "varno varattno vartype vartypmod varcollid varnullingrels varlevelsup varnosyn "
        "varattnosyn location"
    ),
    "CONST": "consttype consttypmod constcollid constlen constbyval constisnull location constvalue",
    "PARAM": "paramkind paramid paramtype paramtypmod paramcollid location",
    "FUNCEXPR": (
        "funcid funcresulttype funcretset funcvariadic funcformat funccollid inputcollid args "
        "location"
    ),
    "OPEXPR": "opno opfuncid opresulttype opretset opcollid inputcollid args location",
    "DISTINCTEXPR": "opno opfuncid opresulttype opretset opcollid inputcollid args location",
    "NULLIFEXPR": "opno opfuncid opresulttype opretset opcollid inputcollid args location",
    "BOOLEXPR": "boolop args location",
    "NULLTEST": "arg nulltesttype argisrow location",
    "BOOLEANTEST": "arg booltesttype location",
    "RELABELTYPE": "arg resulttype resulttypmod resultcollid relabelformat location",
    "COERCEVIAIO": "arg resulttype resultcollid coerceformat location",
    "COLLATEEXPR": "arg collOid location",
    "CASEEXPR": "casetype casecollid arg args defresult location",
    "CASEWHEN": "expr result location",
    "CASETESTEXPR": "typeId typeMod collation",
    "COALESCEEXPR": "coalescetype coalescecollid args location",
    "MINMAXEXPR": "minmaxtype minmaxcollid inputcollid op args location",
    "SQLVALUEFUNCTION": "op type typmod location",
    "SUBLINK": "subLinkType subLinkId testexpr operName subselect location",
    "COMMONTABLEEXPR": (
        "ctename aliascolnames ctematerialized ctequery search_clause cycle_clause location "
        "cterecursive cterefcount ctecolnames ctecoltypes ctecoltypmods ctecolcollations"
    ),
    "SORTGROUPCLAUSE": "tleSortGroupRef eqop sortop nulls_first hashable",
    "SETOPERATIONSTMT": "op all larg rarg colTypes colTypmods colCollations groupClauses",
    "GROUPINGSET": "kind content location",
    "AGGREF": (
        "aggfnoid aggtype aggcollid inputcollid aggtranstype aggargtypes aggdirectargs args "
        "aggorder aggdistinct aggfilter aggstar aggvariadic aggkind aggpresorted agglevelsup "
        "aggsplit aggno aggtransno location"
    ),
    "WINDOWFUNC": (
        "winfnoid wintype wincollid inputcollid args aggfilter runCondition winref winstar "
        "winagg location"
    ),
    "WINDOWCLAUSE": (
        "name refname partitionClause orderClause frameOptions startOffset endOffset "
        "startInRangeFunc endInRangeFunc inRangeColl inRangeAsc inRangeNullsFirst winref "
        "copiedOrder"
    ),
}
FIELDS = {tag: set(fields.split()) for tag, fields in FIELDS.items()}
TYPE_FIELDS = set(
    """
vartype consttype paramtype funcresulttype opresulttype resulttype casetype typeId
coalescetype minmaxtype type aggtype aggtranstype aggargtypes wintype coltypes ctecoltypes colTypes
""".split()
)
FUNCTION_FIELDS = {"funcid", "aggfnoid", "winfnoid"}
SUPPORT_FIELDS = {"opfuncid", "startInRangeFunc", "endInRangeFunc"}
OPERATOR_FIELDS = {"opno", "eqop", "sortop"}
TYPE_LIST_FIELDS = {"aggargtypes", "coltypes", "ctecoltypes", "colTypes"}
RANGE_FIELDS = {
    0: "relid inh relkind rellockmode perminfoindex tablesample",
    1: "subquery security_barrier relid inh relkind rellockmode perminfoindex",
    2: "jointype joinmergedcols joinaliasvars joinleftcols joinrightcols join_using_alias",
    5: "values_lists coltypes coltypmods colcollations",
    6: "ctename ctelevelsup self_reference coltypes coltypmods colcollations",
    8: "",
}

SymbolLookup = Callable[[set[tuple[str, int]]], dict[tuple[str, int], dict]]


def review_expressions(contract: dict, lookup: SymbolLookup) -> Decision:
    """Review protocol-2 trees using symbols from the same locked remote snapshot.

    VALUE covers expression dependencies only. It does not authorize a source read,
    certify storage extensions, or replace guard, mapping and privilege checks.
    """
    if contract.get("protocol") != 2 or contract.get("server_version", 0) // 10000 != 17:
        return Decision.unknown("Stored expression review requires protocol 2 and PostgreSQL 17")
    review = _Review(lookup)
    try:
        relations = contract["relations"]
        review.relations = {item["oid"] for item in relations}
        if not relations or contract["oid"] not in review.relations:
            raise ValueError("Source is absent from the guarded relation closure")
        for relation in relations:
            if relation["kind"] == "v":
                review.tree(relation["view_tree"], query=True)
            for expression in relation["expressions"]:
                review.tree(expression["tree"])
            for policy in relation["policies"]:
                for field in ("using_tree", "check_tree"):
                    if policy[field] is not None:
                        review.tree(policy[field])
        review.resolve()
    except NodeLimitError as exc:
        return Decision.unexecuted(str(exc), OperationState.BLOCKED_BY_BUDGET)
    except (ValueError, TypeError, KeyError) as exc:
        return Decision.unexecuted(str(exc), OperationState.FAILED)
    raw = {
        "relations": sorted(review.referenced_relations),
        "symbols": [dict(kind=kind, oid=oid) for kind, oid in sorted(review.resolved)],
        "issues": sorted(review.issues),
    }
    if review.issues:
        return Decision.unknown("Remote expression dependencies are not fully admitted", raw=raw)
    return Decision.known(True, raw=raw)


class _Review:
    def __init__(self, lookup):
        self.lookup = lookup
        self.relations = set()
        self.referenced_relations = set()
        self.pending = set()
        self.resolved = {}
        self.direct_functions = set()
        self.issues = set()
        self.characters = 0

    def symbol(self, kind, oid):
        if type(oid) is not int or oid < 0:
            raise ValueError("Invalid resolved catalog identifier")
        if oid:
            self.pending.add((kind, oid))

    def tree(self, source, *, query=False):
        self.characters += len(source)
        if self.characters > 1_000_000:
            raise NodeLimitError("Remote expressions exceed the shared character budget")
        root = read_tree(source)
        nodes = root if isinstance(root, tuple) else (root,)
        if not nodes or any(not isinstance(node, Node) for node in nodes):
            raise ValueError("Stored expression must contain PostgreSQL nodes")
        if query and (len(nodes) != 1 or nodes[0].tag != "QUERY"):
            raise ValueError("Stored view must contain one PostgreSQL query")
        self.walk(root)

    def walk(self, item):
        if isinstance(item, tuple):
            for child in item:
                self.walk(child)
            return
        if not isinstance(item, Node):
            return
        tag, fields = item.tag, item.fields
        expected = FIELDS.get(tag)
        if tag == "RANGETBLENTRY":
            expected = set("alias eref rtekind lateral inFromCl securityQuals".split())
            expected.update(RANGE_FIELDS.get(fields.get("rtekind"), "").split())
        if expected is None or fields.keys() != expected:
            self.issues.add(f"Unsupported PostgreSQL node shape: {tag}")
            return
        if tag == "QUERY" and (
            fields["commandType"] != 1
            or fields["utilityStmt"] is not None
            or fields["hasModifyingCTE"]
            or fields["rowMarks"] is not None
            or fields["resultRelation"] != 0
        ):
            self.issues.add("Stored expression contains a modifying or locking query")
        if tag == "RANGETBLENTRY":
            if fields["rtekind"] not in {0, 1, 2, 5, 6, 8} or fields.get("tablesample"):
                self.issues.add("Unsupported range-table source")
            if fields["rtekind"] == 0:
                relation = fields["relid"]
                self.referenced_relations.add(relation)
                if relation not in self.relations:
                    self.issues.add("Expression references a relation outside the guarded closure")
        if tag == "SQLVALUEFUNCTION" and fields["op"] not in set(range(15)):
            self.issues.add("Unsupported SQL value function")
        if tag == "PARAM" and fields["paramkind"] != 2:
            self.issues.add("Stored expression requires an external parameter")
        if tag in {"FUNCEXPR", "OPEXPR", "DISTINCTEXPR", "NULLIFEXPR"} and (
            fields.get("funcretset") or fields.get("opretset")
        ):
            self.issues.add("Set-returning expressions need a separate population contract")
        for field, value in fields.items():
            if field in TYPE_FIELDS:
                if field in TYPE_LIST_FIELDS:
                    if value is not None and not isinstance(value, TypedList):
                        raise ValueError("Invalid resolved type list")
                    values = value.values if value is not None else ()
                else:
                    values = (value,)
                for oid in values:
                    if oid and oid not in SCALAR_TYPES:
                        self.issues.add(f"Unsupported expression type: {oid}")
                    self.symbol("type", oid)
            elif field in FUNCTION_FIELDS | SUPPORT_FIELDS:
                self.symbol("function", value)
                if field in FUNCTION_FIELDS:
                    self.direct_functions.add(value)
            elif field in OPERATOR_FIELDS:
                self.symbol("operator", value)
            self.walk(value)

    def resolve(self):
        while missing := self.pending - self.resolved.keys():
            if len(self.pending) > 512:
                raise NodeLimitError("Remote expressions exceed the symbol budget")
            found = self.lookup(missing)
            for key in sorted(missing):
                definition = found.get(key)
                self.resolved[key] = definition
                if definition is None:
                    self.issues.add(f"Missing remote symbol: {key[0]} {key[1]}")
                    continue
                kind, oid = key
                if not 0 < oid < 10000 or definition["namespace"] != 11:
                    self.issues.add(f"Non-core remote symbol: {kind} {oid}")
                    continue
                if kind == "function":
                    self.function(oid, definition)
                elif kind == "operator":
                    self.operator(oid, definition)
                elif kind == "type":
                    if oid not in SCALAR_TYPES or definition["kind"] != "b":
                        self.issues.add(f"Unsupported expression type: {oid}")
                    for function in definition["functions"]:
                        self.symbol("function", function)

    def function(self, oid, definition):
        if (
            definition["language"] != 12
            or definition["security_definer"]
            or definition["config"] is not None
            or definition["defaults"]
            or definition["library"] is not None
        ):
            self.issues.add(f"Unsupported function implementation: {oid}")
        if oid in self.direct_functions and (
            definition["name"] not in FUNCTIONS
            or definition["returns_set"]
            or definition["volatility"] not in {"i", "s"}
        ):
            self.issues.add(f"Unreviewed function: {definition['name']} ({oid})")
        self.symbol("function", definition["support"])
        aggregate = definition["aggregate"]
        if aggregate:
            if aggregate["kind"] != "n":
                self.issues.add("Ordered-set aggregates need a separate expression contract")
            for function in aggregate["functions"]:
                self.symbol("function", function)
            self.symbol("operator", aggregate["sort_operator"])

    def operator(self, oid, definition):
        if definition["name"] not in OPERATORS or any(
            definition[field] not in SCALAR_TYPES | {0} for field in ("left", "right", "result")
        ):
            self.issues.add(f"Unreviewed operator: {oid}")
        for field in ("function", "restriction", "join"):
            self.symbol("function", definition[field])
