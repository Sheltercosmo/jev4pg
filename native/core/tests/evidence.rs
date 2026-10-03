use jev_executor::evidence::{Observation, Policy, context_identity, request_identity};
use jev_executor::{Decision, Provider};
use serde_json::{Value, json};

fn capture(source: &Value, instructions: &str) -> Observation {
    let questions =
        serde_json::from_value(json!({"q":{"type":"noul","instructions":instructions}})).unwrap();
    let provider = Provider {
        endpoint: "https://provider.example/v1/systemone".into(),
        model: "test-v1".into(),
        revision: "revision-3".into(),
        api_key: "never-export-this".into(),
    };
    Observation::capture(
        source,
        &questions,
        &provider,
        json!({
            "model":"test-v1", "answers":{"q":{"type":"noul","noul":0.7}}, "usage":{"tokens":23}
        }),
    )
    .unwrap()
}

#[test]
fn policy_replay_preserves_raw_evidence_and_provider_identity() {
    for text in [
        "Has the task finished?",
        "Is the work complete?",
        "任务完成了吗？",
    ] {
        let source = json!({"说明":"已完成", "amount":null});
        let evidence = capture(&source, text);
        assert!(matches!(
            evidence.decide(&source, Policy::default()).unwrap()["q"],
            Decision::Unknown { .. }
        ));
        assert_eq!(
            evidence
                .decide(
                    &source,
                    Policy {
                        accept: 0.65,
                        reject: 0.2
                    }
                )
                .unwrap()["q"]
                .require_bool(),
            Ok(true)
        );
        let saved = serde_json::to_string(&evidence).unwrap();
        assert!(!saved.contains("never-export-this"));
        assert!(!saved.contains("provider.example"));
        let restored: Observation = serde_json::from_str(&saved).unwrap();
        assert_eq!(restored, evidence);
        assert_eq!(restored.response["usage"]["tokens"], 23);
    }
}

#[test]
fn source_identity_is_exact_but_independent_of_decimal_spelling_and_key_order() {
    let hash = |text| context_identity(&serde_json::from_str(text).unwrap()).unwrap();
    assert_eq!(
        hash(r#"{"n":1e-7,"a":-0.00}"#),
        hash(r#"{"a":0,"n":0.000000100}"#)
    );
    assert_eq!(hash(r#"{"n":1.234e3}"#), hash(r#"{"n":1234.000}"#));
    assert_eq!(hash(r#"{"n":-1.23000e-2}"#), hash(r#"{"n":-0.0123}"#));
    assert_ne!(
        hash(r#"{"n":900719925474099312345.123456789}"#),
        hash(r#"{"n":900719925474099312345.123456788}"#)
    );
    for other in [
        r#"{"n":"1"}"#,
        r#"{"n":true}"#,
        r#"{"n":null}"#,
        r#"{"other":1}"#,
        r#"{"n":[1]}"#,
        r#"{}"#,
    ] {
        assert_ne!(hash(r#"{"n":1}"#), hash(other));
    }
}

#[test]
fn changed_context_and_invalid_evidence_cannot_be_replayed() {
    let source = json!({"note":"done"});
    let evidence = capture(&source, "Complete?");
    for changed in [
        json!({"note":"pending"}),
        json!({"renamed":"done"}),
        json!({}),
        Value::Null,
    ] {
        assert!(evidence.decide(&changed, Policy::default()).is_err());
    }
    let mut changed = evidence.clone();
    changed.format_version = 2;
    assert!(changed.decide(&source, Policy::default()).is_err());
    changed = evidence.clone();
    changed.evaluator.model = "different-model".into();
    assert!(changed.decide(&source, Policy::default()).is_err());
    changed = evidence.clone();
    changed.response["answers"]["q"]["noul"] = json!(true);
    assert!(changed.decide(&source, Policy::default()).is_err());
    for policy in [
        Policy {
            accept: 0.1,
            reject: 0.2,
        },
        Policy {
            accept: f64::NAN,
            reject: 0.2,
        },
    ] {
        assert!(evidence.decide(&source, policy).is_err());
    }
}

#[test]
fn automatic_reuse_requires_the_expected_question_and_pinned_evaluator() {
    let source = json!({"说明":"完成", "amount":null});
    let evidence = capture(&source, "任务完成了吗？");
    let provider = Provider {
        endpoint: "https://provider.example/v1/systemone".into(),
        model: "test-v1".into(),
        revision: "revision-3".into(),
        api_key: "rotated-credential".into(),
    };
    let expected = request_identity(&source, &evidence.questions, &provider).unwrap();
    assert_eq!(evidence.identity().unwrap(), expected);
    assert!(
        evidence
            .decide_for(&source, &evidence.questions, &provider, Policy::default())
            .is_ok()
    );
    for (field, value) in [
        ("endpoint", "https://other.example/v1/systemone"),
        ("model", "v2"),
        ("revision", "revision-4"),
    ] {
        let mut changed = provider.clone();
        match field {
            "endpoint" => changed.endpoint = value.into(),
            "model" => changed.model = value.into(),
            _ => changed.revision = value.into(),
        }
        assert_ne!(
            request_identity(&source, &evidence.questions, &changed).unwrap(),
            expected
        );
        assert!(
            evidence
                .decide_for(&source, &evidence.questions, &changed, Policy::default())
                .is_err()
        );
    }
    let mut questions = evidence.questions.clone();
    questions.get_mut("q").unwrap().instructions = json!("Has the work started?");
    assert!(
        evidence
            .decide_for(&source, &questions, &provider, Policy::default())
            .is_err()
    );
    questions = evidence.questions.clone();
    questions.get_mut("q").unwrap().subject_column = Some("说明".into());
    assert!(
        evidence
            .decide_for(&source, &questions, &provider, Policy::default())
            .is_err()
    );
    let renamed = json!({"description":"完成", "amount":null});
    assert!(
        evidence
            .decide_for(&renamed, &evidence.questions, &provider, Policy::default())
            .is_err()
    );
    let mut corrupt = evidence;
    corrupt.response["answers"]["q"]["noul"] = json!(true);
    assert_eq!(corrupt.identity().unwrap(), expected);
    assert!(
        corrupt
            .decide_for(&source, &corrupt.questions, &provider, Policy::default())
            .is_err()
    );
}
