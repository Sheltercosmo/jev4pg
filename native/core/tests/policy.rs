use jev_executor::evidence::{Observation, Policy};
use jev_executor::{Decision, Limits, Provider};
use serde_json::{Value, json};

fn observation(question: Value, answer: Value) -> Observation {
    Observation::capture(
        &json!({"说明": "已记录"}),
        &serde_json::from_value(json!({"q": question})).unwrap(),
        &Provider {
            endpoint: "https://example.test/evaluate".into(),
            model: "fixture".into(),
            revision: "1".into(),
            api_key: String::new(),
        },
        json!({"model": "fixture", "answers": {"q": answer}}),
    )
    .unwrap()
}

fn decide(observation: &Observation, policy: Policy) -> Decision {
    observation
        .decide(&json!({"说明": "已记录"}), policy)
        .unwrap()
        .remove("q")
        .unwrap()
}

#[test]
fn declared_uncertainty_is_policy_not_an_english_reserved_word() {
    for (selected, definition) in [
        ("unknown", "Insufficient evidence"),
        ("未知", "信息不足，无法判断"),
        ("none", "No charge applies"),
    ] {
        let evidence = observation(
            json!({"type":"choice", "instructions":"Choose the applicable outcome.",
                "criteria": {selected: definition, "other_value":"Another resolved outcome"}}),
            json!({"type":"choice", "choice":selected,
                "probabilities":{selected:0.9, "other_value":0.1}}),
        );
        assert!(
            matches!(decide(&evidence, Policy::default()), Decision::Value { value, .. } if value == selected)
        );
        let original = evidence.clone();
        let policy = Policy {
            unknown_options: vec![selected.into()],
            ..Policy::default()
        };
        assert!(matches!(
            decide(&evidence, policy),
            Decision::Unknown { .. }
        ));
        assert_eq!(evidence, original);
    }
}

#[test]
fn choice_and_score_thresholds_replay_without_replacing_raw_observations() {
    let choice = observation(
        json!({"type":"choice", "instructions":"Select.", "criteria":{"a":"A","b":"B"}}),
        json!({"type":"choice", "choice":"a", "probabilities":{"a":0.7,"b":0.3}}),
    );
    assert!(matches!(
        decide(
            &choice,
            Policy {
                choice_min: 0.7,
                ..Policy::default()
            }
        ),
        Decision::Value { .. }
    ));
    assert!(matches!(
        decide(
            &choice,
            Policy {
                choice_min: 0.8,
                ..Policy::default()
            }
        ),
        Decision::Unknown { .. }
    ));
    for confidence in [None, Some(0.4), Some(0.6)] {
        let mut answer = json!({"type":"score", "score":0.7,
            "probabilities":{"0":0.3,"1":0.7}, "legend":{"0":"Normal","1":"Urgent"}});
        if let Some(confidence) = confidence {
            answer["confidence"] = json!(confidence);
        }
        let score = observation(
            json!({"type":"score", "instructions":"Rate.", "criteria":["Normal","Urgent"]}),
            answer,
        );
        let result = decide(
            &score,
            Policy {
                score_confidence_min: 0.6,
                ..Policy::default()
            },
        );
        assert_eq!(
            matches!(result, Decision::Value { .. }),
            confidence == Some(0.6)
        );
    }
}

#[test]
fn invalid_policies_cannot_enter_execution_or_replay() {
    for options in [
        json!({"choice_min":1.1}),
        json!({"score_confidence_min":-0.1}),
        json!({"unknown_options":[""]}),
        json!({"policy_revision":" "}),
    ] {
        let limits: Limits = serde_json::from_value(options).unwrap();
        assert!(limits.validate().is_err());
    }
    for invalid in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
        assert!(
            Policy {
                choice_min: invalid,
                ..Policy::default()
            }
            .validate()
            .is_err()
        );
        assert!(
            Policy {
                score_confidence_min: invalid,
                ..Policy::default()
            }
            .validate()
            .is_err()
        );
    }
}
