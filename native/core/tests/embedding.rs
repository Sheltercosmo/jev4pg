use jev_executor::{
    Decision, Decisions,
    embedding::{Basis, distance},
    evidence::{EvaluatorIdentity, Policy},
    resolve,
};
use serde_json::{Value, json};

fn basis() -> Value {
    json!({"context_columns":["body"],"questions":{
        "action":{"type":"noul","instructions":"Does this request action?","subject_column":"body"},
        "topic":{"type":"choice","instructions":"Choose a topic","criteria":{"a":"General","b":"Support","c":"Other"}}
    }})
}

fn evaluator() -> Option<EvaluatorIdentity> {
    Some(EvaluatorIdentity {
        endpoint_sha256: "a".repeat(64),
        model: "model".into(),
        revision: "v1".into(),
    })
}

fn answers(p: f64, distribution: Value) -> Value {
    json!({"model":"model","answers":{
        "action":{"type":"noul","noul":p},
        "topic":{"type":"choice","choice":"a","probabilities":distribution}
    }})
}

#[test]
fn probabilities_remain_available_when_the_categorical_decision_is_unknown() {
    let basis = Basis::parse(basis()).unwrap();
    let decisions = resolve(
        &answers(0.5, json!({"a":0.4,"b":0.35,"c":0.25})),
        &basis.questions,
        "model",
        &Policy::default(),
    )
    .unwrap();
    let result = basis.project(&decisions, evaluator()).unwrap();
    assert!(result.complete);
    assert_eq!(result.output_state, "VALUE");
    assert_eq!(
        result.probabilities,
        vec![
            vec![Some(0.5), Some(0.5)],
            vec![Some(0.4), Some(0.35), Some(0.25)]
        ]
    );
    assert_eq!(
        result.vector,
        vec![Some(0.5), Some(0.5), Some(0.4), Some(0.35), Some(0.25)]
    );
    assert!(
        result
            .question_states
            .iter()
            .all(|s| s.decision_state == "UNKNOWN" && s.output_state == "VALUE")
    );
    assert_eq!(distance(&result, &result).unwrap(), 0.0);
}

#[test]
fn basis_order_is_stable_but_changes_to_meaning_or_context_are_not_compatible() {
    let first = Basis::parse(basis())
        .unwrap()
        .project(&Decisions::new(), evaluator())
        .unwrap();
    let reordered = Basis::parse(serde_json::from_str(r#"{"questions":{"topic":{"criteria":{"c":"Other","b":"Support","a":"General"},"instructions":"Choose a topic","type":"choice"},"action":{"subject_column":"body","instructions":"Does this request action?","type":"noul"}},"context_columns":["body"]}"#).unwrap()).unwrap().project(&Decisions::new(), evaluator()).unwrap();
    assert_eq!(first.basis_id, reordered.basis_id);
    assert_eq!(first.axes, reordered.axes);
    for (path, value) in [
        (
            vec!["questions", "action", "instructions"],
            json!("Does this describe a finished action?"),
        ),
        (
            vec!["questions", "topic", "criteria", "a"],
            json!("Purchase request"),
        ),
        (vec!["context_columns"], json!(["body", "language"])),
    ] {
        let mut changed = basis();
        let mut target = &mut changed;
        for key in path {
            target = &mut target[key];
        }
        *target = value;
        let second = Basis::parse(changed)
            .unwrap()
            .project(&Decisions::new(), evaluator())
            .unwrap();
        assert_ne!(first.basis_id, second.basis_id);
    }
}

#[test]
fn unexecuted_questions_keep_null_cells_and_the_original_operational_state() {
    let basis = Basis::parse(basis()).unwrap();
    let mut decisions = resolve(
        &answers(1.0, json!({"a":1.0,"b":0.0,"c":0.0})),
        &basis.questions,
        "model",
        &Policy::default(),
    )
    .unwrap();
    for operation in ["SKIPPED", "FAILED", "BLOCKED_BY_BUDGET", "TRUNCATED"] {
        decisions.insert(
            "topic".into(),
            Decision::blocked(operation, "not available"),
        );
        let matrix = basis.project(&decisions, evaluator()).unwrap();
        assert!(!matrix.complete);
        assert_eq!(matrix.probabilities[0], vec![Some(0.0), Some(1.0)]);
        assert_eq!(matrix.probabilities[1], vec![None, None, None]);
        assert_eq!(matrix.question_states[1].operation_state, operation);
        assert_eq!(matrix.question_states[1].decision_state, "NOT_EVALUATED");
        assert!(distance(&matrix, &matrix).unwrap_err().contains("Complete"));
    }
    decisions.remove("topic");
    assert_eq!(
        basis
            .project(&decisions, evaluator())
            .unwrap()
            .question_states[1]
            .operation_state,
        "BLOCKED_BY_DEPENDENCY"
    );
}

#[test]
fn projection_can_reuse_a_subset_of_a_shared_question_batch() {
    let full = Basis::parse(basis()).unwrap();
    let decisions = resolve(
        &answers(0.5, json!({"a":0.4,"b":0.35,"c":0.25})),
        &full.questions,
        "model",
        &Policy::default(),
    )
    .unwrap();
    let mut definition = basis();
    definition["questions"]
        .as_object_mut()
        .unwrap()
        .remove("topic");
    let subset = Basis::parse(definition).unwrap();
    let matrix = subset.project(&decisions, evaluator()).unwrap();
    assert!(matrix.complete);
    assert_eq!(matrix.probabilities, vec![vec![Some(0.5), Some(0.5)]]);
    assert_ne!(
        matrix.basis_id,
        full.project(&decisions, evaluator()).unwrap().basis_id
    );
}

#[test]
fn distance_weights_questions_equally_instead_of_weighting_their_answer_count() {
    let basis = Basis::parse(basis()).unwrap();
    let project = |response| {
        basis
            .project(
                &resolve(&response, &basis.questions, "model", &Policy::default()).unwrap(),
                evaluator(),
            )
            .unwrap()
    };
    let first = project(answers(0.0, json!({"a":1.0,"b":0.0,"c":0.0})));
    let other_binary = project(answers(1.0, json!({"a":1.0,"b":0.0,"c":0.0})));
    let other_choice = project(answers(0.0, json!({"a":0.0,"b":1.0,"c":0.0})));
    let opposite = project(answers(1.0, json!({"a":0.0,"b":1.0,"c":0.0})));
    assert_eq!(distance(&first, &first).unwrap(), 0.0);
    assert_eq!(distance(&first, &opposite).unwrap(), 1.0);
    assert!((distance(&first, &other_binary).unwrap() - 0.5_f64.sqrt()).abs() < 1e-12);
    assert_eq!(
        distance(&first, &other_binary).unwrap(),
        distance(&first, &other_choice).unwrap()
    );
    assert_eq!(
        distance(&other_binary, &first).unwrap(),
        distance(&first, &other_binary).unwrap()
    );
}

#[test]
fn distances_reject_incompatible_or_corrupt_matrices_without_silent_truncation() {
    let basis = Basis::parse(basis()).unwrap();
    let decisions = resolve(
        &answers(0.5, json!({"a":0.5,"b":0.5,"c":0.0})),
        &basis.questions,
        "model",
        &Policy::default(),
    )
    .unwrap();
    let first = basis.project(&decisions, evaluator()).unwrap();
    for mutate in [
        |m: &mut jev_executor::embedding::AnswerMatrix| {
            m.evaluator.as_mut().unwrap().revision = "v2".into();
        },
        |m: &mut jev_executor::embedding::AnswerMatrix| {
            m.basis_id = "b".repeat(64);
        },
        |m: &mut jev_executor::embedding::AnswerMatrix| {
            m.probabilities[0][0] = None;
        },
        |m: &mut jev_executor::embedding::AnswerMatrix| {
            m.vector.pop();
        },
        |m: &mut jev_executor::embedding::AnswerMatrix| {
            m.axes[0].answers.swap(0, 1);
        },
        |m: &mut jev_executor::embedding::AnswerMatrix| {
            m.evaluator = None;
        },
        |m: &mut jev_executor::embedding::AnswerMatrix| {
            m.probabilities.pop();
        },
    ] {
        let mut changed = first.clone();
        mutate(&mut changed);
        assert!(distance(&first, &changed).is_err());
    }
}

#[test]
fn score_distributions_keep_level_order_and_low_confidence_probabilities() {
    let basis = Basis::parse(json!({"context_columns":["说明"],"questions":{"紧急度":{
        "type":"score","instructions":"评估紧急程度","criteria":["正常","需要关注","紧急"]
    }}}))
    .unwrap();
    let response = json!({"model":"model","answers":{"紧急度":{"type":"score","score":1.2,"confidence":0.1,"legend":{"0":"正常","1":"需要关注","2":"紧急"},"probabilities":{"2":0.4,"0":0.2,"1":0.4}}}});
    let policy = Policy {
        score_confidence_min: 0.8,
        ..Policy::default()
    };
    let decisions = resolve(&response, &basis.questions, "model", &policy).unwrap();
    let result = basis.project(&decisions, evaluator()).unwrap();
    assert_eq!(result.axes[0].answers, ["0", "1", "2"]);
    assert_eq!(result.vector, [Some(0.2), Some(0.4), Some(0.4)]);
    assert_eq!(result.question_states[0].decision_state, "UNKNOWN");
    assert!(result.complete);
}

#[test]
fn invalid_bases_and_forged_probability_distributions_are_rejected() {
    for fields in [json!([]), json!(["body", "body"]), json!(["wrong"])] {
        let mut value = basis();
        value["context_columns"] = fields;
        assert!(Basis::parse(value).is_err());
    }
    let basis = Basis::parse(basis()).unwrap();
    for p in [json!(-0.1), json!(1.1), json!(null), json!("0.5")] {
        let decisions: Decisions = serde_json::from_value(json!({"action":{"output_state":"UNKNOWN","operation_state":"SUCCEEDED","raw":{"type":"noul","noul":p}}})).unwrap();
        assert!(basis.project(&decisions, evaluator()).is_err());
    }
}
