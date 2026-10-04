"""Expression metadata admission without source rows, providers or live servers."""

from copy import deepcopy

import pytest

from sdd.generic.pg_nodes import Datum, NodeLimitError, TypedList, read_tree
from sdd.generic.remote_expressions import review_expressions


def policy_contract(tree):
    return {
        "protocol": 2,
        "server_version": 170007,
        "oid": 20000,
        "relations": [
            {
                "oid": 20000,
                "kind": "r",
                "expressions": [],
                "policies": [{"using_tree": tree, "check_tree": None}],
            }
        ],
    }


CURRENT_USER = "{SQLVALUEFUNCTION :op 10 :type 19 :typmod -1 :location -1}"


def test_token_escaping_preserves_names_without_interpreting_them():
    node = read_tree(
        r'{ALIAS :aliasname \{FUNCEXPR\ \:funcid\ 99999\} :colnames ("说明\ 内容" "a\\b" "\<\>")}'
    )
    assert node.fields["aliasname"] == "{FUNCEXPR :funcid 99999}"
    assert node.fields["colnames"] == ("说明 内容", "a\\b", "<>")


def test_constant_bytes_are_opaque():
    node = read_tree("{CONST :constvalue 2 [ 123 -1 0 0 0 0 0 0 ]}")
    assert node.fields["constvalue"] == Datum(2, (123, -1, 0, 0, 0, 0, 0, 0))
    assert read_tree("(o 23 1700)") == TypedList("o", (23, 1700))


@pytest.mark.parametrize("name", [":id", "[", "]", "说明\u2003内容"])
def test_names_use_postgresql_token_rules(name):
    assert read_tree("{ALIAS :aliasname " + name + " :colnames <>}").fields["aliasname"] == name


def test_escaped_quote_is_part_of_identifier():
    assert (
        read_tree(r"{ALIAS :aliasname \"quoted\" :colnames <>}").fields["aliasname"] == '"quoted"'
    )


@pytest.mark.parametrize(
    "source",
    [
        "",
        "{",
        "{VAR :vartype}",
        "{VAR :vartype 23 :vartype 25}",
        "{CONST :constvalue 9 [ 0 ]}",
        "{CONST :constvalue 1 [ 256 ]}",
        "(o 23 false)",
        "{VAR :vartype 23} ignored",
        "unfinished\\",
    ],
)
def test_invalid_tree_is_rejected(source):
    with pytest.raises(ValueError):
        read_tree(source)


def test_parser_budgets():
    with pytest.raises(NodeLimitError):
        read_tree(" " * 100, max_chars=99)
    with pytest.raises(NodeLimitError):
        read_tree("(1 2 3)", max_tokens=4)
    with pytest.raises(NodeLimitError):
        read_tree("(" * 100 + "1" + ")" * 100)


def test_shared_states_keep_unreviewed_and_failed_separate():
    symbols = {("type", 19): {"namespace": 11, "kind": "b", "functions": []}}
    result = review_expressions(policy_contract(CURRENT_USER), lambda _: symbols)
    assert result.output_state == "VALUE" and result.value is True
    missing = review_expressions(policy_contract(CURRENT_USER), lambda _: {})
    assert missing.output_state == "UNKNOWN" and missing.operation_state == "SUCCEEDED"
    malformed = review_expressions(policy_contract("{CONST :constvalue"), lambda _: symbols)
    assert malformed.output_state == "NOT_EVALUATED" and malformed.operation_state == "FAILED"
    blocked = review_expressions(policy_contract("(" * 100 + "1" + ")" * 100), lambda _: symbols)
    assert (
        blocked.output_state == "NOT_EVALUATED" and blocked.operation_state == "BLOCKED_BY_BUDGET"
    )


@pytest.mark.parametrize("version", [160009, 180001])
def test_other_server_layouts_are_not_inferred(version):
    contract = policy_contract(CURRENT_USER)
    contract["server_version"] = version
    result = review_expressions(contract, lambda _: pytest.fail("Unneeded symbol query"))
    assert result.output_state == "UNKNOWN"


@pytest.mark.parametrize(
    "tree",
    [
        CURRENT_USER.replace(" :type 19", ""),
        CURRENT_USER.replace("}", " :futureFunction 90000}"),
        "{COERCETODOMAIN :resulttype 90000}",
    ],
)
def test_unknown_shapes_are_not_silently_accepted(tree):
    result = review_expressions(policy_contract(tree), lambda _: {})
    assert result.output_state == "UNKNOWN"


def test_scalar_metadata_is_not_an_expression():
    result = review_expressions(policy_contract("17"), lambda _: {})
    assert result.output_state == "NOT_EVALUATED" and result.operation_state == "FAILED"


def test_review_does_not_modify_contract():
    contract = policy_contract(CURRENT_USER)
    before = deepcopy(contract)
    review_expressions(contract, lambda _: {})
    assert contract == before
