import pytest

from test_native_relational import compile_query
from sdd.generic.native_relational import needs_relational_plan


def test_case_uses_a_shared_identity_and_selective_completeness():
    tree, plan = compile_query(
        "SELECT CASE WHEN id=1 THEN SEMANTIC(note,'Complete?') ELSE false END AS done FROM items"
    )
    assert needs_relational_plan(tree)
    source = plan["stages"][0]
    assert "ROW_NUMBER()" in source["sql"] and source["keys"]
    semantic = next(stage for stage in plan["stages"] if stage["operator"] == "semantic")
    assert semantic["row_guard"]["equals"] is True
    assert semantic["context_columns"] == ["id", "note", "p"]
    assert all(source["require_values"] == [] for source in semantic["inputs"])
    merged = next(stage for stage in plan["stages"] if stage["operator"] == "merge")
    assert merged["selections"]
    assert "require_values" not in plan["stages"][-1]["inputs"][0]


def test_independent_questions_share_one_group_and_selected_branches_stay_parallel():
    _, plan = compile_query("""SELECT SEMANTIC(note,'Independent?') AS independent,
        CASE WHEN SEMANTIC(note,'Route?') THEN SEMANTIC(note,'Urgent?') ELSE SEMANTIC(note,'Routine?') END AS answer FROM items""")
    semantic = [stage for stage in plan["stages"] if stage["operator"] == "semantic"]
    assert len(semantic) == 3
    unconditional = next(stage for stage in semantic if "row_guard" not in stage)
    assert len(unconditional["questions"]) == 2
    branches = [stage for stage in semantic if "row_guard" in stage]
    assert branches[0]["inputs"] == branches[1]["inputs"]
    assert not any(branches[0]["id"] == parent["stage"] for parent in branches[1]["inputs"])


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT CASE id WHEN 1 THEN SEMANTIC(note,'First?') WHEN 2 THEN SEMANTIC(note,'Second?') ELSE false END AS answer FROM items",
        "SELECT CASE WHEN id<3 THEN CASE WHEN p>0.8 THEN SEMANTIC(note,'First?') ELSE false END ELSE SEMANTIC(note,'Second?') END AS answer FROM items",
        "SELECT CASE WHEN false AND SEMANTIC(note,'First?') THEN true ELSE false END AS answer FROM items",
        "SELECT SUM(CASE WHEN id=1 THEN CASE WHEN SEMANTIC(note,'Complete?') THEN 1 ELSE 0 END ELSE 0 END) AS n FROM items",
    ],
)
def test_conditional_shapes_lower_without_unbound_semantic_calls(sql):
    _, plan = compile_query(sql)
    assert "SEMANTIC(" not in " ".join(stage["sql"] for stage in plan["stages"])
    by_id = {stage["id"]: stage for stage in plan["stages"]}
    assert all(parent["stage"] in by_id for stage in plan["stages"] for parent in stage["inputs"])
    assert all(stage["keys"] for stage in plan["stages"][:-1])


def test_window_conditions_require_a_real_predecessor_relation():
    with pytest.raises(ValueError, match="CTE"):
        compile_query(
            "SELECT CASE WHEN ROW_NUMBER() OVER(ORDER BY id)=1 THEN SEMANTIC(note,'Complete?') ELSE false END AS answer FROM items"
        )
