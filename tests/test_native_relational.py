import pytest
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

from sdd.generic.native_relational import compile_relational_plan, needs_relational_plan


def compile_query(sql):
    schema = {"items": {"id": "INT", "note": "TEXT", "p": "DECIMAL"}}
    tree = qualify(
        sqlglot.parse_one(sql, read="postgres"), dialect="postgres", schema=schema, identify=True
    )
    dataset = {
        "primary_key": ["id"],
        "columns": [
            {"name": "id", "type": "integer"},
            {"name": "note", "type": "text"},
            {"name": "p", "type": "number"},
        ],
    }
    bindings = {
        id(source): dataset
        for scope in traverse_scope(tree)
        for _, source in scope.selected_sources.values()
        if isinstance(source, exp.Table)
    }
    return tree, compile_relational_plan(
        tree, bindings, lambda query: query.sql(dialect="postgres")
    )


def test_derived_semantic_scope_follows_its_sql_parent():
    tree, plan = compile_query(
        "WITH grouped AS (SELECT note,MAX(p) AS confidence FROM items GROUP BY note) SELECT note FROM grouped WHERE SEMANTIC(note,'Complete?')"
    )
    assert needs_relational_plan(tree)
    semantic = next(stage for stage in plan["stages"] if stage["operator"] == "semantic")
    parent = next(
        stage for stage in plan["stages"] if stage["id"] == semantic["inputs"][0]["stage"]
    )
    assert "GROUP BY" in parent["sql"] and "MAX(" in parent["sql"]
    assert set(semantic["columns"]) == {"note", "confidence"}
    assert "SEMANTIC(" not in " ".join(stage["sql"] for stage in plan["stages"])
    assert plan["stages"][-1]["inputs"] == [{"stage": semantic["id"], "alias": semantic["id"]}]


def test_shared_cte_and_independent_semantic_branches_remain_a_dag():
    _, plan = compile_query(
        "WITH common AS (SELECT note,p FROM items), a AS (SELECT note FROM common WHERE SEMANTIC(note,'First?')), b AS (SELECT note FROM common WHERE SEMANTIC(note,'Second?')) SELECT a.note FROM a JOIN b ON a.note=b.note"
    )
    semantic = [stage for stage in plan["stages"] if stage["operator"] == "semantic"]
    assert len(semantic) == 2 and semantic[0]["inputs"] == semantic[1]["inputs"]
    assert sum('FROM "items"' in stage["sql"] for stage in plan["stages"]) == 1


def test_structured_filters_precede_evaluation_and_repeated_questions_share_a_batch():
    _, plan = compile_query(
        "SELECT q.note,SEMANTIC(q.note,'Complete?') AS done FROM (SELECT note,p FROM items) q WHERE p>0.2 AND SEMANTIC(q.note,'Complete?')"
    )
    semantic = next(stage for stage in plan["stages"] if stage["operator"] == "semantic")
    assert len(semantic["questions"]) == 1
    assert "WHERE" in semantic["sql"] and "0.2" in semantic["sql"]
    assert "SEMANTIC" not in semantic["sql"]


def test_semantic_derived_stages_compose_through_union():
    _, plan = compile_query(
        "WITH common AS (SELECT note FROM items) SELECT note FROM common WHERE SEMANTIC(note,'Complete?') UNION ALL SELECT note FROM common WHERE SEMANTIC(note,'Pending?')"
    )
    assert "UNION ALL" in plan["stages"][-1]["sql"]
    assert len(plan["stages"][-1]["inputs"]) == 2


@pytest.mark.parametrize(
    "sql",
    [
        "WITH q(description,confidence) AS (SELECT note,p FROM items) SELECT description FROM q WHERE SEMANTIC(description,'Complete?')",
        "SELECT description FROM (SELECT note,p FROM items) q(description,confidence) WHERE SEMANTIC(description,'Complete?')",
    ],
)
def test_qualified_derived_column_aliases_keep_their_ordinal_meaning(sql):
    _, plan = compile_query(sql)
    semantic = next(stage for stage in plan["stages"] if stage["operator"] == "semantic")
    assert set(semantic["columns"]) == {"description", "confidence"}
    assert '"note" AS "description"' in plan["stages"][0]["sql"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT q.note FROM (SELECT note,id FROM items) q WHERE EXISTS(SELECT 1 FROM items b WHERE b.id=q.id AND SEMANTIC(q.note,'Complete?'))",
        "SELECT SEMANTIC(CONCAT(q.note,'x'),'Complete?') FROM (SELECT note FROM items) q",
    ],
)
def test_unsupported_dependency_shapes_are_rejected_before_execution(sql):
    with pytest.raises(ValueError):
        compile_query(sql)
