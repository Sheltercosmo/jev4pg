use jev_executor::{
    Decision,
    conditional::{RowGuard, Selection},
};
use serde_json::{Value, json};

fn value(value: Value) -> Value {
    json!({"output_state":"VALUE","operation_state":"SUCCEEDED","value":value,"raw":{}})
}

fn state(decision: Decision) -> Value {
    serde_json::to_value(decision).unwrap()
}

#[test]
fn row_conditions_preserve_unknown_missing_failed_and_typed_values() {
    let guard: RowGuard = serde_json::from_value(json!({"column":"选择","equals":true})).unwrap();
    assert!(guard.evaluate(&json!({"选择":value(json!(true))})).unwrap());
    assert!(
        !guard
            .evaluate(&json!({"选择":value(json!(false))}))
            .unwrap()
    );
    for (input, expected) in [
        (
            json!({"output_state":"UNKNOWN","operation_state":"SUCCEEDED","raw":{}}),
            "BLOCKED_BY_DEPENDENCY",
        ),
        (
            state(Decision::blocked("SKIPPED", "not selected")),
            "BLOCKED_BY_DEPENDENCY",
        ),
        (
            state(Decision::blocked("BLOCKED_BY_BUDGET", "no allowance")),
            "BLOCKED_BY_DEPENDENCY",
        ),
        (
            state(Decision::blocked("FAILED", "transport")),
            "BLOCKED_BY_DEPENDENCY",
        ),
        (json!(null), "FAILED"),
        (value(json!(1)), "BLOCKED_BY_POLICY"),
        (value(json!([true])), "BLOCKED_BY_POLICY"),
    ] {
        let result = state(guard.evaluate(&json!({"选择":input})).unwrap_err());
        assert_eq!(result["output_state"], "NOT_EVALUATED");
        assert_eq!(result["operation_state"], expected);
    }
}

#[test]
fn selected_branch_retains_its_state_and_ignores_unselected_work() {
    let selection: Selection = serde_json::from_value(json!({"selector":"route", "cases":[
        {"equals":true,"column":"yes"},{"equals":false,"column":"no"}
    ]}))
    .unwrap();
    for chosen in [
        value(json!(false)),
        json!({"output_state":"UNKNOWN","operation_state":"SUCCEEDED","raw":{"p":0.5}}),
        state(Decision::blocked("BLOCKED_BY_BUDGET", "empty")),
    ] {
        let source = json!({"route":value(json!(true)),"yes":chosen,"no":null});
        assert_eq!(state(selection.evaluate(&source)), chosen);
    }
    assert_eq!(
        state(selection.evaluate(&json!({"route":null,"yes":value(json!(true))})))["output_state"],
        "NOT_EVALUATED"
    );
    assert_eq!(
        state(
            selection
                .evaluate(&json!({"route":value(json!(false)),"yes":value(json!(true)),"no":null}))
        )["operation_state"],
        "FAILED"
    );
}

#[test]
fn exact_numeric_and_choice_selectors_use_only_the_selected_alternative() {
    let selection: Selection = serde_json::from_str(r#"{"selector":"route","cases":[{"equals":9007199254740993,"column":"exact"}],"otherwise":"other"}"#).unwrap();
    let exact: Value = serde_json::from_str("9007199254740993.000").unwrap();
    let source = json!({"route":value(exact),"exact":value(json!(42)),"other":value(json!(0))});
    assert_eq!(state(selection.evaluate(&source))["value"], 42);
    let choice: Selection = serde_json::from_value(
        json!({"selector":"路径","cases":[{"equals":"复查","column":"review"}]}),
    )
    .unwrap();
    assert_eq!(
        state(choice.evaluate(&json!({"路径":value(json!("复查")),"review":value(json!(true))})))["value"],
        true
    );
    assert_eq!(
        state(choice.evaluate(&json!({"路径":value(json!("未覆盖"))})))["operation_state"],
        "BLOCKED_BY_POLICY"
    );
}
