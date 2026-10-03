import pytest
import sqlglot

from sdd.generic.native_plan import native_plan
from sdd.generic.stage_dag import Column, StageDAG, operation, ref


def graph():
    dag = StageDAG()
    source = dag.source(
        sqlglot.parse_one('SELECT "说明" FROM items', read="postgres"),
        {"说明": Column("text", "Work description")},
    )
    target = dag.aggregate(source, [], {"count": operation("count", ref("说明"))})
    return dag, source, target


def test_native_lowering_keeps_sql_types_and_shared_dependencies():
    dag, source, target = graph()
    questions = {source: {"done": {"type": "noul", "instructions": "是否完成？"}}}
    plan = native_plan(dag, target, questions=questions)
    assert plan["target"] == target and len(plan["stages"]) == 2
    assert plan["stages"][0]["operator"] == "semantic"
    assert plan["stages"][1]["inputs"] == [
        {"stage": source, "alias": source, "require_values": ["done"]}
    ]
    assert plan["stages"][1]["keys"] == ((),)
    assert plan["stages"][0]["columns"]["说明"]["kind"] == "text"
    assert plan["stages"][1]["sql"] == dag.nodes[target].query.sql(dialect="postgres")
    plan["stages"][0]["questions"]["done"]["instructions"] = "changed"
    assert questions[source]["done"]["instructions"] == "是否完成？"


def test_explicit_sealed_input_does_not_require_unrelated_decisions():
    dag, source, target = graph()
    plan = native_plan(
        dag,
        target,
        questions={source: {"done": {"type": "noul", "instructions": "Complete?"}}},
        requirements={(target, source): []},
    )
    assert plan["stages"][-1]["inputs"][0]["require_values"] == []


@pytest.mark.parametrize(
    "options",
    [
        {"questions": {"missing": {"done": {}}}},
        {"requirements": {("missing", "missing"): []}},
        {"requirements": {("stage_1", "stage_0"): ["missing"]}},
        {"guards": {"stage_1": {"input": "missing"}}},
    ],
)
def test_declarations_cannot_introduce_hidden_graph_dependencies(options):
    dag, _, target = graph()
    with pytest.raises(ValueError):
        native_plan(dag, target, **options)
