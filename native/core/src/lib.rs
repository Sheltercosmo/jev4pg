//! Typed semantic evaluation. PostgreSQL owns source selection and arithmetic.

pub mod evidence;
pub mod source;

use evidence::{Observation, Policy};

use futures_util::{StreamExt, stream};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::{BTreeMap, HashMap};
use std::time::Duration;

pub type Questions = BTreeMap<String, Question>;
pub type Decisions = BTreeMap<String, Decision>;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Question {
    #[serde(rename = "type")]
    pub kind: String,
    pub instructions: Value,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub criteria: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub subject_column: Option<String>,
}

pub fn validate_questions(questions: &Questions) -> Result<(), &'static str> {
    if questions.is_empty() || questions.len() > 32 {
        return Err("Supply 1 to 32 typed questions");
    }
    for (id, q) in questions {
        let has_instructions = match &q.instructions {
            Value::String(text) => !text.trim().is_empty(),
            Value::Object(fields) => !fields.is_empty(),
            Value::Array(items) => !items.is_empty(),
            _ => false,
        };
        if id.is_empty()
            || id.len() > 200
            || !has_instructions
            || q.subject_column
                .as_ref()
                .is_some_and(|name| name.is_empty())
        {
            return Err("Question identity and instructions are required");
        }
        match q.kind.as_str() {
            "noul" => {}
            "choice"
                if q.criteria
                    .as_ref()
                    .and_then(Value::as_object)
                    .is_some_and(|v| (2..=255).contains(&v.len())) => {}
            "score"
                if q.criteria
                    .as_ref()
                    .and_then(Value::as_array)
                    .is_some_and(|v| (2..=10).contains(&v.len())) => {}
            _ => return Err("Invalid typed question or criteria"),
        }
    }
    Ok(())
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "output_state")]
pub enum Decision {
    #[serde(rename = "VALUE")]
    Value {
        value: Value,
        operation_state: String,
        raw: Value,
    },
    #[serde(rename = "UNKNOWN")]
    Unknown { operation_state: String, raw: Value },
    #[serde(rename = "NOT_EVALUATED")]
    NotEvaluated {
        operation_state: String,
        reason: String,
    },
}

impl Decision {
    pub fn blocked(state: &str, reason: &str) -> Self {
        Self::NotEvaluated {
            operation_state: state.into(),
            reason: reason.into(),
        }
    }

    pub fn require_bool(&self) -> Result<bool, &'static str> {
        match self {
            Self::Value {
                value: Value::Bool(value),
                operation_state,
                ..
            } if operation_state == "SUCCEEDED" => Ok(*value),
            Self::Value { .. } => Err("The decision is not Boolean"),
            _ => Err("An unresolved semantic decision cannot determine exact membership"),
        }
    }
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Provider {
    pub endpoint: String,
    pub model: String,
    pub revision: String,
    #[serde(default)]
    pub api_key: String,
}

#[derive(Clone, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Limits {
    pub max_rows: usize,
    pub max_judgments: usize,
    pub max_requests: usize,
    pub max_input_bytes: usize,
    pub batch_rows: usize,
    pub concurrency: usize,
    pub timeout_ms: u64,
    pub accept: f64,
    pub reject: f64,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            max_rows: 10_000,
            max_judgments: 1_000,
            max_requests: 1_000,
            max_input_bytes: 8_000_000,
            batch_rows: 32,
            concurrency: 4,
            timeout_ms: 45_000,
            accept: 0.8,
            reject: 0.2,
        }
    }
}

impl Limits {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.max_rows == 0
            || self.max_rows > 1_000_000
            || self.max_judgments > 1_000_000
            || self.max_requests > 100_000
            || self.max_input_bytes > 128_000_000
            || !(1..=32).contains(&self.batch_rows)
            || !(1..=16).contains(&self.concurrency)
            || !(1..=120_000).contains(&self.timeout_ms)
            || !self.accept.is_finite()
            || !self.reject.is_finite()
            || !(0.0 <= self.reject && self.reject < self.accept && self.accept <= 1.0)
        {
            return Err("Invalid native execution limits");
        }
        Ok(())
    }
}

#[derive(Default, Serialize)]
pub struct Usage {
    pub requests: usize,
    pub judgments: usize,
    pub input_bytes: usize,
    pub reused_rows: usize,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Evaluation {
    pub decisions: Decisions,
    pub observation: Option<Observation>,
}

impl Evaluation {
    fn unexecuted(questions: &Questions, state: &str, reason: &str) -> Self {
        Self {
            decisions: unexecuted(questions, state, reason),
            observation: None,
        }
    }
}

pub struct Executor {
    provider: Provider,
    pub limits: Limits,
    client: reqwest::Client,
    runtime: tokio::runtime::Runtime,
    cache: HashMap<String, Evaluation>,
    cache_bytes: usize,
    pub usage: Usage,
}

impl Executor {
    pub fn new(provider: Provider, limits: Limits) -> Result<Self, &'static str> {
        limits.validate()?;
        let endpoint =
            reqwest::Url::parse(&provider.endpoint).map_err(|_| "Invalid provider endpoint")?;
        if !["http", "https"].contains(&endpoint.scheme())
            || !endpoint.username().is_empty()
            || endpoint.password().is_some()
            || endpoint.query().is_some()
            || endpoint.fragment().is_some()
            || provider.model.is_empty()
            || provider.revision.is_empty()
        {
            return Err("Use a pinned provider, model and revision without credentials in its URL");
        }
        let client = reqwest::Client::builder()
            .hickory_dns(true)
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_millis(limits.timeout_ms))
            .connect_timeout(Duration::from_secs(5))
            .build()
            .map_err(|_| "Provider client initialization failed")?;
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|_| "Native scheduler initialization failed")?;
        Ok(Self {
            provider,
            limits,
            client,
            runtime,
            cache: HashMap::new(),
            cache_bytes: 0,
            usage: Usage::default(),
        })
    }

    pub fn evaluate(
        &mut self,
        rows: &[Value],
        questions: &Questions,
        mut check_interrupt: impl FnMut(),
    ) -> Result<Vec<Evaluation>, &'static str> {
        validate_questions(questions)?;
        if rows.len() > self.limits.batch_rows {
            return Err("Source batch exceeds batch_rows");
        }
        let mut results: Vec<Option<Evaluation>> = vec![None; rows.len()];
        let mut aliases: HashMap<String, Vec<usize>> = HashMap::new();
        let mut requests = Vec::new();
        let mut output_bytes = 0;
        for (index, row) in rows.iter().enumerate() {
            let (active, skipped) = applicable_questions(row, questions)?;
            if active.is_empty() {
                results[index] = Some(Evaluation {
                    decisions: skipped,
                    observation: None,
                });
                continue;
            }
            let key =
                serde_json::to_string(&(row, questions)).map_err(|_| "Invalid semantic input")?;
            if let Some(cached) = self.cache.get(&key) {
                output_bytes += serde_json::to_vec(cached)
                    .map_err(|_| "Invalid cached result")?
                    .len();
                if output_bytes > 8_000_000 {
                    return Err("Semantic result batch exceeds 8 MB; reduce batch_rows");
                }
                results[index] = Some(cached.clone());
                self.usage.reused_rows += 1;
                continue;
            }
            if let Some(indices) = aliases.get_mut(&key) {
                indices.push(index);
                self.usage.reused_rows += 1;
                continue;
            }
            aliases.insert(key.clone(), vec![index]);
            let mut provider_questions =
                serde_json::to_value(&active).map_err(|_| "Invalid questions")?;
            for question in provider_questions.as_object_mut().unwrap().values_mut() {
                question.as_object_mut().unwrap().remove("subject_column");
            }
            let payload = json!({"model": self.provider.model, "state": row, "questions": provider_questions});
            let bytes = serde_json::to_vec(&payload)
                .map_err(|_| "Invalid semantic input")?
                .len();
            if bytes > 1_000_000 {
                return Err("One semantic input exceeds the 1 MB context limit");
            }
            if self.usage.judgments + active.len() > self.limits.max_judgments
                || self.usage.requests + 1 > self.limits.max_requests
                || self.usage.input_bytes + bytes > self.limits.max_input_bytes
            {
                let mut held = Evaluation::unexecuted(
                    &active,
                    "BLOCKED_BY_BUDGET",
                    "Native query budget exhausted",
                );
                held.decisions.extend(skipped);
                results[index] = Some(held);
                continue;
            }
            self.usage.judgments += active.len();
            self.usage.requests += 1;
            self.usage.input_bytes += bytes;
            requests.push((key, payload, row, active, skipped));
        }
        let provider = &self.provider;
        let client = &self.client;
        let limits = &self.limits;
        let cache = &mut self.cache;
        let cache_bytes = &mut self.cache_bytes;
        self.runtime.block_on(async {
            let pending = stream::iter(requests.into_iter().map(|(key, payload, row, active, skipped)| async move {
                let response = send(client, provider, payload).await.and_then(|value| {
                    let observation = Observation::capture(row, &active, provider, value)?;
                    let decisions = observation.decide(row, Policy { accept: limits.accept, reject: limits.reject })?;
                    Ok(Evaluation { decisions, observation: Some(observation) })
                });
                let mut evaluation = response.unwrap_or_else(|reason| Evaluation::unexecuted(&active, "FAILED", reason));
                evaluation.decisions.extend(skipped);
                (key, evaluation)
            })).buffer_unordered(limits.concurrency);
            futures_util::pin_mut!(pending);
            let mut tick = tokio::time::interval(Duration::from_millis(25));
            loop {
                tokio::select! {
                    item = pending.next() => {
                        let Some((key, decisions)) = item else { break; };
                        let bytes = serde_json::to_vec(&decisions).map_err(|_| "Invalid result")?.len();
                        output_bytes += bytes * aliases[&key].len();
                        if output_bytes > 8_000_000 {
                            return Err("Semantic result batch exceeds 8 MB; reduce batch_rows");
                        }
                        if decisions.observation.is_some() {
                            let entry_bytes = key.len() + bytes;
                            if *cache_bytes + entry_bytes > 8_000_000 { cache.clear(); *cache_bytes = 0; }
                            if entry_bytes <= 8_000_000 {
                                cache.insert(key.clone(), decisions.clone()); *cache_bytes += entry_bytes;
                            }
                        }
                        for index in &aliases[&key] { results[*index] = Some(decisions.clone()); }
                    },
                    _ = tick.tick() => check_interrupt(),
                }
            }
            Ok::<_, &'static str>(())
        })?;
        for indices in aliases.values() {
            if let Some(result) = results[indices[0]].clone() {
                for index in indices {
                    results[*index].get_or_insert_with(|| result.clone());
                }
            }
        }
        results
            .into_iter()
            .map(|v| v.ok_or("Native execution lost a result identity"))
            .collect()
    }
}

fn applicable_questions(
    row: &Value,
    questions: &Questions,
) -> Result<(Questions, Decisions), &'static str> {
    let row = row
        .as_object()
        .ok_or("Semantic input must be a projected row object")?;
    let mut active = Questions::new();
    let mut skipped = Decisions::new();
    for (id, question) in questions {
        if let Some(column) = &question.subject_column {
            let value = row
                .get(column)
                .ok_or("Subject column is absent from the context")?;
            if value.is_null() {
                skipped.insert(
                    id.clone(),
                    Decision::blocked("SKIPPED", "Subject value is NULL"),
                );
                continue;
            }
        }
        active.insert(id.clone(), question.clone());
    }
    Ok((active, skipped))
}

fn unexecuted(questions: &Questions, state: &str, reason: &str) -> Decisions {
    questions
        .keys()
        .map(|id| (id.clone(), Decision::blocked(state, reason)))
        .collect()
}

async fn send(
    client: &reqwest::Client,
    provider: &Provider,
    payload: Value,
) -> Result<Value, &'static str> {
    let mut request = client.post(&provider.endpoint).json(&payload);
    if !provider.api_key.is_empty() {
        request = request.bearer_auth(&provider.api_key);
    }
    let mut response = request
        .send()
        .await
        .map_err(|_| "Provider transport failed")?;
    if !response.status().is_success() {
        return Err("Provider rejected the request");
    }
    let mut bytes = Vec::new();
    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|_| "Provider response failed")?
    {
        if bytes.len() + chunk.len() > 4_000_000 {
            return Err("Provider response exceeds limit");
        }
        bytes.extend_from_slice(&chunk);
    }
    serde_json::from_slice(&bytes).map_err(|_| "Provider response is not valid JSON")
}

fn probability(value: &Value) -> Result<f64, &'static str> {
    value
        .as_f64()
        .filter(|v| v.is_finite() && (0.0..=1.0).contains(v))
        .ok_or("Invalid probability")
}

pub fn resolve(
    response: &Value,
    questions: &Questions,
    model: &str,
    accept: f64,
    reject: f64,
) -> Result<Decisions, &'static str> {
    Policy { accept, reject }.validate()?;
    validate_questions(questions)?;
    if response["model"] != model {
        return Err("Response model does not match");
    }
    if let Some(usage) = response.get("usage")
        && !usage
            .as_object()
            .is_some_and(|counts| counts.values().all(|v| v.as_u64().is_some()))
    {
        return Err("Invalid provider usage");
    }
    let answers = response["answers"].as_object().ok_or("Missing answers")?;
    if answers.len() != questions.len() || questions.keys().any(|id| !answers.contains_key(id)) {
        return Err("Response question identities do not match");
    }
    questions
        .iter()
        .map(|(id, q)| {
            let raw = &answers[id];
            if raw["type"] != q.kind {
                return Err("Response primitive does not match");
            }
            if let Some(confidence) = raw.get("confidence") {
                probability(confidence)?;
            }
            let value = match q.kind.as_str() {
                "noul" => {
                    let p = probability(&raw["noul"])?;
                    if p >= accept {
                        Some(json!(true))
                    } else if p <= reject {
                        Some(json!(false))
                    } else {
                        None
                    }
                }
                "choice" | "score" => {
                    let expected: Vec<String> = if q.kind == "choice" {
                        q.criteria
                            .as_ref()
                            .and_then(Value::as_object)
                            .ok_or("Invalid choice options")?
                            .keys()
                            .cloned()
                            .collect()
                    } else {
                        (0..q
                            .criteria
                            .as_ref()
                            .and_then(Value::as_array)
                            .ok_or("Invalid score levels")?
                            .len())
                            .map(|i| i.to_string())
                            .collect()
                    };
                    let ps = raw["probabilities"]
                        .as_object()
                        .ok_or("Missing probabilities")?;
                    if ps.len() != expected.len()
                        || expected.iter().any(|key| !ps.contains_key(key))
                    {
                        return Err("Probability options do not match");
                    }
                    let sum: f64 = ps
                        .values()
                        .map(probability)
                        .collect::<Result<Vec<_>, _>>()?
                        .iter()
                        .sum();
                    if (sum - 1.0).abs() > 0.03 {
                        return Err("Probabilities do not sum to one");
                    }
                    if q.kind == "choice" {
                        let selected = raw["choice"].as_str().ok_or("Missing selected option")?;
                        if !ps.contains_key(selected) {
                            return Err("Selected option is outside criteria");
                        }
                        if probability(&ps[selected])? < 0.55 {
                            None
                        } else {
                            Some(json!(selected))
                        }
                    } else {
                        let score = raw["score"]
                            .as_f64()
                            .filter(|v| {
                                v.is_finite() && *v >= 0.0 && *v <= (expected.len() - 1) as f64
                            })
                            .ok_or("Invalid score")?;
                        let levels = q.criteria.as_ref().unwrap().as_array().unwrap();
                        let legend = raw["legend"].as_object().ok_or("Missing score legend")?;
                        if legend.len() != levels.len()
                            || expected
                                .iter()
                                .enumerate()
                                .any(|(i, key)| legend.get(key) != Some(&levels[i]))
                        {
                            return Err("Score legend changed");
                        }
                        Some(json!(score))
                    }
                }
                _ => return Err("Unsupported primitive"),
            };
            let decision = match value {
                Some(value) => Decision::Value {
                    value,
                    operation_state: "SUCCEEDED".into(),
                    raw: raw.clone(),
                },
                None => Decision::Unknown {
                    operation_state: "SUCCEEDED".into(),
                    raw: raw.clone(),
                },
            };
            Ok((id.clone(), decision))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn false_unknown_and_not_evaluated_are_distinct() {
        let questions =
            serde_json::from_value(json!({"q":{"type":"noul","instructions":"完成了吗？"}}))
                .unwrap();
        let response = |p: Value| json!({"model":"m1","answers":{"q":{"type":"noul","noul":p}}});
        let no = resolve(&response(json!(0.05)), &questions, "m1", 0.8, 0.2).unwrap();
        assert_eq!(no["q"].require_bool(), Ok(false));
        assert!(
            resolve(&response(json!(0.5)), &questions, "m1", 0.8, 0.2).unwrap()["q"]
                .require_bool()
                .is_err()
        );
        assert!(
            Decision::blocked("SKIPPED", "Branch not taken")
                .require_bool()
                .is_err()
        );
        assert!(resolve(&response(json!(true)), &questions, "m1", 0.8, 0.2).is_err());
    }
}
