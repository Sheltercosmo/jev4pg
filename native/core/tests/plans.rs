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

#[test]
fn validates_row_conditions_and_selected_decision_dependencies() {
    let mut source = stage("route", json!([]));
    source["columns"]["decision"] = json!({"kind":"json","label":"route"});
    source["operator"] = json!("semantic");
    source["questions"] = json!({"done":{"type":"noul","instructions":"Review"}});
    source["row_guard"] = json!({"column":"decision","equals":true});
    let mut merge = stage(
        "merge",
        json!([{"stage":"route","alias":"route","require_values":[]}]),
    );
    merge["operator"] = json!("merge");
    merge["columns"]["selector"] = json!({"kind":"json","label":"routing"});
    merge["columns"]["answer"] = json!({"kind":"json","label":"answer"});
    merge["selections"] =
        json!({"result":{"selector":"selector","cases":[{"equals":true,"column":"answer"}]}});
    let child = stage(
        "end",
        json!([{"stage":"merge","alias":"m","require_values":["result"]}]),
    );
    let plan = json!({"version":1,"target":"end","stages":[source,merge,child]});
    assert!(Plan::parse(plan.clone()).is_ok());
    for (field, invalid) in [
        ("column", json!("missing")),
        ("equals", json!(null)),
        ("equals", json!([])),
    ] {
        let mut wrong = plan.clone();
        wrong["stages"][0]["row_guard"][field] = invalid;
        assert!(Plan::parse(wrong).is_err());
    }
    for cases in [
        json!([]),
        json!([{"equals":true,"column":"missing"}]),
        json!([{"equals":1,"column":"answer"},{"equals":1.0,"column":"answer"}]),
        json!([{"equals":true,"column":"answer"},{"equals":1,"column":"answer"}]),
    ] {
        let mut wrong = plan.clone();
        wrong["stages"][1]["selections"]["result"]["cases"] = cases;
        assert!(Plan::parse(wrong).is_err());
    }
}

#[test]
fn context_projection_excludes_internal_fields_without_losing_subjects() {
    let mut input = stage("input", json!([]));
    input["operator"] = json!("semantic");
    input["columns"]["route"] = json!({"kind":"json","label":"Routing"});
    input["questions"] = json!({"q":{"type":"noul","instructions":"Check","subject_column":"id"}});
    input["row_guard"] = json!({"column":"route","equals":true});
    input["context_columns"] = json!(["id"]);
    let plan = json!({"version":1,"target":"input","stages":[input]});
    assert!(Plan::parse(plan.clone()).is_ok());
    for columns in [
        json!([]),
        json!(["route"]),
        json!(["id", "id"]),
        json!(["id", "missing"]),
        json!(["id", "route"]),
    ] {
        let mut invalid = plan.clone();
        invalid["stages"][0]["context_columns"] = columns;
        assert!(Plan::parse(invalid).is_err());
    }
}
