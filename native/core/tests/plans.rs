use jev_executor::plan::{Plan, same_scalar};
use serde_json::{Value, json};

#[test]
fn scalar_guards_compare_exact_numbers_without_coercing_booleans() {
    let parse = |value: &str| serde_json::from_str::<Value>(value).unwrap();
    assert!(same_scalar(&parse("1.000"), &parse("1e0")).unwrap());
    assert!(!same_scalar(&json!(true), &json!(1)).unwrap());
    assert!(!same_scalar(&parse("9007199254740993"), &parse("9007199254740992")).unwrap());
}

fn stage(id: &str, inputs: Value) -> Value {
    json!({"id": id, "operator": "source", "sql": "SELECT 1 AS id",
        "columns": {"id": {"kind": "integer", "label": "编号"}}, "inputs": inputs})
}

#[test]
fn validates_shared_graphs_and_scalar_grain() {
    let mut source = stage("起点", json!([]));
    source["keys"] = json!([[]]);
    let left = stage("left", json!([{"stage": "起点", "alias": "a"}]));
    let right = stage("right", json!([{"stage": "起点", "alias": "b"}]));
    let target = stage(
        "end",
        json!([{"stage":"left", "alias":"l"},{"stage":"right","alias":"r"}]),
    );
    assert!(
        Plan::parse(json!({"version":1,"target":"end","stages":[source,left,right,target]}))
            .is_ok()
    );
}

#[test]
fn rejects_invalid_dependencies_before_execution() {
    for (stages, target) in [
        (
            vec![stage("a", json!([{"stage":"missing","alias":"x"}]))],
            "a",
        ),
        (
            vec![
                stage("a", json!([{"stage":"b","alias":"b"}])),
                stage("b", json!([{"stage":"a","alias":"a"}])),
            ],
            "a",
        ),
        (vec![stage("a", json!([])), stage("b", json!([]))], "a"),
        (vec![stage("a", json!([])), stage("a", json!([]))], "a"),
    ] {
        assert!(Plan::parse(json!({"version":1,"target":target,"stages":stages})).is_err());
    }
}

#[test]
fn validates_typed_decision_requirements_and_guards() {
    let mut source = stage("a", json!([]));
    source["operator"] = json!("semantic");
    source["questions"] = json!({"complete":{"type":"noul","instructions":"完成了吗？"}});
    let mut child = stage(
        "b",
        json!([{"stage":"a","alias":"a","require_values":["complete"]}]),
    );
    child["guard"] = json!({"input":"a","question":"complete","equals":true});
    let mut plan = json!({"version":1,"target":"b","stages":[source,child]});
    assert!(Plan::parse(plan.clone()).is_ok());
    plan["stages"][1]["guard"]["question"] = json!("missing");
    assert!(Plan::parse(plan.clone()).is_err());
    plan["stages"][1].as_object_mut().unwrap().remove("guard");
    plan["stages"][1]["inputs"][0]["require_values"] = json!(["missing"]);
    assert!(Plan::parse(plan).is_err());
}
