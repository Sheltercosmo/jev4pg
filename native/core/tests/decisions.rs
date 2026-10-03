use jev_executor::evidence::Policy;
use jev_executor::{Decision, Questions, resolve, validate_questions};
use serde_json::{Value, json};

fn question(value: Value) -> Questions {
    serde_json::from_value(json!({"q": value})).unwrap()
}

fn envelope(answer: Value) -> Value {
    json!({"model":"test-v1", "answers":{"q":answer}})
}

#[test]
fn instruction_contract_rejects_empty_or_nonlinguistic_values() {
    for instructions in [
        Value::Null,
        json!("  "),
        json!({}),
        json!([]),
        json!(false),
        json!(7),
    ] {
        assert!(
            validate_questions(&question(
                json!({"type":"noul","instructions":instructions})
            ))
            .is_err()
        );
    }
    assert!(
        validate_questions(&question(json!({"type":"noul","instructions":"已完成？"}))).is_ok()
    );
}

#[test]
fn choice_labels_are_user_defined_and_not_reserved_words() {
    let questions = question(
        json!({"type":"choice","instructions":"Select the applicable charge.",
        "criteria":{"none":"No charge applies","service":"A service charge applies"}}),
    );
    let response = envelope(
        json!({"type":"choice","choice":"none","probabilities":{"none":0.9,"service":0.1}}),
    );
    let decisions = resolve(&response, &questions, "test-v1", &Policy::default()).unwrap();
    assert!(matches!(&decisions["q"],Decision::Value { value, .. } if value == "none"));
}

#[test]
fn uncertainty_and_invalid_distributions_remain_distinct() {
    let questions =
        question(json!({"type":"choice","instructions":"Choose.","criteria":{"a":"A","b":"B"}}));
    let answer = json!({"type":"choice","choice":"a","probabilities":{"a":0.5,"b":0.5}});
    assert!(matches!(
        &resolve(
            &envelope(answer.clone()),
            &questions,
            "test-v1",
            &Policy::default()
        )
        .unwrap()["q"],
        Decision::Unknown { .. }
    ));
    for invalid in [json!(true), json!(-0.1), json!(1.1), json!("0.5")] {
        let mut changed = answer.clone();
        changed["probabilities"]["a"] = invalid;
        assert!(
            resolve(
                &envelope(changed),
                &questions,
                "test-v1",
                &Policy::default()
            )
            .is_err()
        );
    }
    let mut changed = answer;
    changed["confidence"] = json!(true);
    assert!(
        resolve(
            &envelope(changed),
            &questions,
            "test-v1",
            &Policy::default()
        )
        .is_err()
    );
}

#[test]
fn score_keeps_the_declared_rubric() {
    let questions = question(
        json!({"type":"score","instructions":"Rate urgency.","criteria":["Normal","Urgent"]}),
    );
    let mut answer = json!({"type":"score","score":0.7,"probabilities":{"0":0.3,"1":0.7},"legend":{"0":"Normal","1":"Urgent"}});
    assert!(
        resolve(
            &envelope(answer.clone()),
            &questions,
            "test-v1",
            &Policy::default()
        )
        .is_ok()
    );
    answer["legend"]["0"] = json!("Urgent");
    assert!(resolve(&envelope(answer), &questions, "test-v1", &Policy::default()).is_err());
}

#[test]
fn response_identity_and_usage_are_checked_before_decisions() {
    let questions = question(json!({"type":"noul","instructions":"Complete?"}));
    let answer = envelope(json!({"type":"noul","noul":0.95}));
    for (field, value) in [
        ("model", json!("another-model")),
        ("answers", json!({})),
        ("usage", json!({"tokens":-1})),
        ("usage", json!({"tokens":true})),
    ] {
        let mut changed = answer.clone();
        changed[field] = value;
        assert!(resolve(&changed, &questions, "test-v1", &Policy::default()).is_err());
    }
}

#[test]
fn failed_state_cannot_authorize_membership() {
    let malformed: Decision = serde_json::from_value(
        json!({"output_state":"VALUE","operation_state":"FAILED","value":true,"raw":{}}),
    )
    .unwrap();
    assert!(malformed.require_bool().is_err());
}
