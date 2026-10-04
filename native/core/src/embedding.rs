//! Interpretable embeddings from a fixed basis of independent typed questions.

use crate::evidence::{EvaluatorIdentity, Policy, context_identity};
use crate::{Decision, Decisions, Questions, probability, resolve, validate_questions};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::BTreeSet;

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Definition {
    questions: Questions,
    context_columns: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Axis {
    pub question_id: String,
    pub kind: String,
    pub answers: Vec<String>,
}

pub struct Basis {
    pub questions: Questions,
    pub context_columns: Vec<String>,
    id: String,
    axes: Vec<Axis>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct QuestionState {
    pub output_state: String,
    pub operation_state: String,
    pub decision_state: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AnswerMatrix {
    pub format_version: u8,
    pub basis_id: String,
    pub axes: Vec<Axis>,
    pub probabilities: Vec<Vec<Option<f64>>>,
    pub vector: Vec<Option<f64>>,
    pub question_states: Vec<QuestionState>,
    pub output_state: String,
    pub operation_state: String,
    pub complete: bool,
    pub evaluator: Option<EvaluatorIdentity>,
}

fn valid_evaluator(identity: &EvaluatorIdentity) -> bool {
    !identity.model.is_empty()
        && !identity.revision.is_empty()
        && identity.endpoint_sha256.len() == 64
        && identity
            .endpoint_sha256
            .bytes()
            .all(|c| c.is_ascii_hexdigit())
}

impl Basis {
    pub fn parse(value: Value) -> Result<Self, &'static str> {
        if serde_json::to_vec(&value)
            .map_err(|_| "Invalid embedding basis")?
            .len()
            > 1_000_000
        {
            return Err("Embedding basis exceeds 1 MB");
        }
        let mut definition: Definition = serde_json::from_value(value)
            .map_err(|_| "Supply embedding questions and context_columns")?;
        validate_questions(&definition.questions)?;
        let names: BTreeSet<_> = definition.context_columns.iter().collect();
        if names.is_empty()
            || names.len() > 256
            || names.len() != definition.context_columns.len()
            || names.iter().any(|name| name.is_empty() || name.len() > 200)
            || definition.questions.values().any(|q| {
                q.subject_column
                    .as_ref()
                    .is_some_and(|name| !names.contains(name))
            })
        {
            return Err("Embedding context must name distinct columns and include every subject");
        }
        definition.context_columns.sort();
        let id = context_identity(&json!({"embedding_basis_version":1,"definition":definition}))?;
        let axes: Vec<Axis> = definition
            .questions
            .iter()
            .map(|(id, question)| Axis {
                question_id: id.clone(),
                kind: question.kind.clone(),
                answers: match question.kind.as_str() {
                    "noul" => vec!["false".into(), "true".into()],
                    "choice" => question
                        .criteria
                        .as_ref()
                        .unwrap()
                        .as_object()
                        .unwrap()
                        .keys()
                        .cloned()
                        .collect(),
                    "score" => (0..question
                        .criteria
                        .as_ref()
                        .unwrap()
                        .as_array()
                        .unwrap()
                        .len())
                        .map(|i| i.to_string())
                        .collect(),
                    _ => unreachable!("Question types were validated"),
                },
            })
            .collect();
        if axes
            .iter()
            .flat_map(|axis| &axis.answers)
            .any(|answer| answer.is_empty() || answer.len() > 200)
        {
            return Err("Embedding answer identities must contain 1 to 200 bytes");
        }
        Ok(Self {
            questions: definition.questions,
            context_columns: definition.context_columns,
            id,
            axes,
        })
    }

    pub fn project(
        &self,
        decisions: &Decisions,
        evaluator: Option<EvaluatorIdentity>,
    ) -> Result<AnswerMatrix, &'static str> {
        if evaluator
            .as_ref()
            .is_some_and(|identity| !valid_evaluator(identity))
        {
            return Err("Invalid embedding evaluator identity");
        }
        let mut active = Questions::new();
        let mut answers = serde_json::Map::new();
        let mut states = Vec::new();
        for (id, question) in &self.questions {
            let (state, operation, reason, raw) = match decisions.get(id) {
                Some(Decision::Value {
                    operation_state,
                    raw,
                    ..
                }) => ("VALUE", operation_state.as_str(), None, Some(raw)),
                Some(Decision::Unknown {
                    operation_state,
                    raw,
                }) => ("UNKNOWN", operation_state.as_str(), None, Some(raw)),
                Some(Decision::NotEvaluated {
                    operation_state,
                    reason,
                }) => (
                    "NOT_EVALUATED",
                    operation_state.as_str(),
                    Some(reason.clone()),
                    None,
                ),
                None => (
                    "NOT_EVALUATED",
                    "BLOCKED_BY_DEPENDENCY",
                    Some("Question was not evaluated".into()),
                    None,
                ),
            };
            if let Some(raw) = raw {
                if operation != "SUCCEEDED" {
                    return Err("Only successful observations can supply probabilities");
                }
                active.insert(id.clone(), question.clone());
                answers.insert(id.clone(), raw.clone());
            }
            states.push(QuestionState {
                output_state: if raw.is_some() {
                    "VALUE"
                } else {
                    "NOT_EVALUATED"
                }
                .into(),
                operation_state: operation.into(),
                decision_state: state.into(),
                reason,
            });
        }
        if !active.is_empty() {
            resolve(
                &json!({"model":"probability-projection","answers":answers}),
                &active,
                "probability-projection",
                &Policy::default(),
            )?;
        }
        let probabilities = self
            .axes
            .iter()
            .map(|axis| {
                let Some(raw) = answers.get(&axis.question_id) else {
                    return Ok(vec![None; axis.answers.len()]);
                };
                if axis.kind == "noul" {
                    let p = probability(&raw["noul"])?;
                    Ok(vec![Some(1.0 - p), Some(p)])
                } else {
                    axis.answers
                        .iter()
                        .map(|answer| probability(&raw["probabilities"][answer]).map(Some))
                        .collect()
                }
            })
            .collect::<Result<Vec<Vec<_>>, &'static str>>()?;
        let complete = active.len() == self.questions.len();
        Ok(AnswerMatrix {
            format_version: 1,
            basis_id: self.id.clone(),
            axes: self.axes.clone(),
            vector: probabilities.iter().flatten().copied().collect(),
            probabilities,
            question_states: states,
            complete,
            output_state: if complete { "VALUE" } else { "NOT_EVALUATED" }.into(),
            operation_state: if complete {
                "SUCCEEDED"
            } else {
                "BLOCKED_BY_DEPENDENCY"
            }
            .into(),
            evaluator,
        })
    }
}

impl AnswerMatrix {
    fn validate_complete(&self) -> Result<(), &'static str> {
        if !self.complete || self.output_state != "VALUE" || self.operation_state != "SUCCEEDED" {
            return Err("Complete probability matrices are required for distance");
        }
        if self.format_version != 1
            || self.basis_id.len() != 64
            || !self.basis_id.bytes().all(|c| c.is_ascii_hexdigit())
            || !(1..=32).contains(&self.axes.len())
            || self.probabilities.len() != self.axes.len()
            || self.question_states.len() != self.axes.len()
            || self
                .evaluator
                .as_ref()
                .is_none_or(|identity| !valid_evaluator(identity))
        {
            return Err("Invalid probability matrix identity or shape");
        }
        let mut ids = BTreeSet::new();
        for ((axis, probabilities), state) in self
            .axes
            .iter()
            .zip(&self.probabilities)
            .zip(&self.question_states)
        {
            let names: BTreeSet<_> = axis.answers.iter().collect();
            if axis.question_id.is_empty()
                || !ids.insert(&axis.question_id)
                || !(2..=255).contains(&names.len())
                || names.len() != axis.answers.len()
                || probabilities.len() != axis.answers.len()
                || state.output_state != "VALUE"
                || state.operation_state != "SUCCEEDED"
                || !["VALUE", "UNKNOWN"].contains(&state.decision_state.as_str())
                || match axis.kind.as_str() {
                    "noul" => axis.answers != ["false", "true"],
                    "choice" => false,
                    "score" => {
                        axis.answers.len() > 10
                            || axis
                                .answers
                                .iter()
                                .enumerate()
                                .any(|(i, name)| *name != i.to_string())
                    }
                    _ => true,
                }
            {
                return Err("Invalid probability matrix axes or states");
            }
            if probabilities
                .iter()
                .any(|p| p.is_none_or(|p| !p.is_finite() || !(0.0..=1.0).contains(&p)))
                || (probabilities.iter().map(|p| p.unwrap_or(0.0)).sum::<f64>() - 1.0).abs() > 0.03
            {
                return Err("Invalid probability distribution");
            }
        }
        if self.vector
            != self
                .probabilities
                .iter()
                .flatten()
                .copied()
                .collect::<Vec<_>>()
        {
            return Err("Embedding vector does not match its probability matrix");
        }
        Ok(())
    }
}

pub fn distance(left: &AnswerMatrix, right: &AnswerMatrix) -> Result<f64, &'static str> {
    left.validate_complete()?;
    right.validate_complete()?;
    if left.basis_id != right.basis_id
        || left.axes != right.axes
        || left.evaluator != right.evaluator
    {
        return Err("Embedding distance requires the same basis and evaluator revision");
    }
    let squared: f64 = left
        .probabilities
        .iter()
        .zip(&right.probabilities)
        .map(|(left, right)| {
            let a: f64 = left.iter().map(|p| p.unwrap()).sum();
            let b: f64 = right.iter().map(|p| p.unwrap()).sum();
            let difference: f64 = left
                .iter()
                .zip(right)
                .map(|(x, y)| ((x.unwrap() / a).sqrt() - (y.unwrap() / b).sqrt()).powi(2))
                .sum();
            (difference / 2.0).clamp(0.0, 1.0)
        })
        .sum();
    Ok((squared / left.axes.len() as f64).sqrt())
}
