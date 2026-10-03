import pytest
import sqlglot
from sqlglot import exp

from sdd.generic.native_sql import _allows_partial_results, _validate_semantic_lineage


@pytest.mark.parametrize(
    "query",
    [
        "SELECT id FROM items WHERE SEMANTIC(note,'Done?')",
        "SELECT SEMANTIC(note,'Done?') AS decision FROM items",
        "SELECT id FROM items WHERE NOT SEMANTIC(note,'Done?')",
        "SELECT id FROM items WHERE SEMANTIC(note,'Done?')=FALSE",
        "SELECT id FROM items WHERE SEMANTIC(note,'Done?') OR id=1",
        "SELECT a.id FROM items a JOIN items b ON a.id=b.id AND SEMANTIC(b.note,'Done?')",
    ],
)
def test_partial_reads_retain_unresolved_values(query):
    tree = sqlglot.parse_one(query, read="postgres")
    operators = [(node,) for node in tree.find_all(exp.Anonymous) if node.name == "SEMANTIC"]
    assert _allows_partial_results(tree, operators)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT id FROM items EXCEPT SELECT id FROM items WHERE SEMANTIC(note,'Done?')",
        "SELECT id FROM items WHERE COALESCE(SEMANTIC(note,'Done?'),FALSE)=FALSE",
        "SELECT id FROM items WHERE SEMANTIC(note,'Done?') IS NOT TRUE",
        "SELECT id FROM items WHERE SEMANTIC(note,'Done?') IS NULL",
        "SELECT id FROM items WHERE SEMANTIC(note,'Done?') ORDER BY id LIMIT 1",
        "SELECT id FROM items WHERE SEMANTIC(note,'Done?') OFFSET 1",
        "SELECT COALESCE(SEMANTIC(note,'Done?'),FALSE) FROM items",
        "SELECT COUNT(*) FROM items WHERE SEMANTIC(note,'Done?')",
        "SELECT DISTINCT id FROM items WHERE SEMANTIC(note,'Done?')",
        "SELECT a.id FROM items a LEFT JOIN items b ON SEMANTIC(b.note,'Done?')",
        "SELECT ROW_NUMBER() OVER (ORDER BY id) FROM items WHERE SEMANTIC(note,'Done?')",
        "SELECT id FROM items WHERE NOT EXISTS (SELECT 1 FROM items b WHERE SEMANTIC(b.note,'Done?'))",
    ],
)
def test_result_shapes_requiring_complete_evidence(query):
    tree = sqlglot.parse_one(query, read="postgres")
    operators = [(node,) for node in tree.find_all(exp.Anonymous) if node.name == "SEMANTIC"]
    assert not _allows_partial_results(tree, operators)


@pytest.mark.parametrize(
    "source,subject",
    [
        ("items a LEFT JOIN other b ON a.id=b.id", "b.note"),
        ("items a RIGHT JOIN other b ON a.id=b.id", "a.note"),
        ("items a FULL JOIN other b ON a.id=b.id", "a.note"),
        ("items a JOIN other b ON a.id=b.id RIGHT JOIN third c ON b.id=c.id", "a.note"),
        ("items a LEFT JOIN (other b) ON a.id=b.id", "b.note"),
        ("(items a LEFT JOIN other b ON a.id=b.id) JOIN third c ON a.id=c.id", "b.note"),
        ("items a JOIN (other b LEFT JOIN third c ON b.id=c.id) ON a.id=b.id", "c.note"),
        ('"事项" a LEFT JOIN "其他" b ON a."编号"=b."编号"', 'b."说明"'),
    ],
)
def test_null_extended_subject_has_no_base_row_observation(source, subject):
    tree = sqlglot.parse_one(
        f"SELECT SEMANTIC({subject},'Complete?') FROM {source}", read="postgres"
    )
    function = next(tree.find_all(exp.Anonymous))
    operators = [(function, None, function.expressions[0].table, None, tree)]
    with pytest.raises(ValueError, match="NULL-extended"):
        _validate_semantic_lineage(operators)


@pytest.mark.parametrize(
    "grouping", ["ROLLUP(id,note)", "CUBE(id,note)", "GROUPING SETS ((id,note),())"]
)
def test_subtotals_cannot_reuse_decisions_for_changed_subjects(grouping):
    tree = sqlglot.parse_one(
        "SELECT id,note,SEMANTIC(note,'Complete?') FROM items GROUP BY " + grouping,
        read="postgres",
    )
    function = next(tree.find_all(exp.Anonymous))
    with pytest.raises(ValueError, match="ordinary GROUP BY"):
        _validate_semantic_lineage([(function, None, "items", None, tree)])
