//! Portable observations and local decision policies. No provider I/O occurs on replay.

use crate::{Decisions, Provider, Questions, resolve, validate_questions};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Policy {
    pub accept: f64,
    pub reject: f64,
}

impl Default for Policy {
    fn default() -> Self {
        Self {
            accept: 0.8,
            reject: 0.2,
        }
    }
}

impl Policy {
    pub fn validate(self) -> Result<(), &'static str> {
        if !(0.0 <= self.reject && self.reject < self.accept && self.accept <= 1.0) {
            return Err("Invalid decision thresholds");
        }
        Ok(())
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EvaluatorIdentity {
    pub endpoint_sha256: String,
    pub model: String,
    pub revision: String,
}

impl From<&Provider> for EvaluatorIdentity {
    fn from(provider: &Provider) -> Self {
        Self {
            endpoint_sha256: format!("{:x}", Sha256::digest(provider.endpoint.as_bytes())),
            model: provider.model.clone(),
            revision: provider.revision.clone(),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Observation {
    pub format_version: u8,
    pub context_sha256: String,
    pub evaluator: EvaluatorIdentity,
    pub questions: Questions,
    pub response: Value,
    pub observed_at_unix_ms: u64,
}

impl Observation {
    pub fn capture(
        source: &Value,
        questions: &Questions,
        provider: &Provider,
        response: Value,
    ) -> Result<Self, &'static str> {
        validate_questions(questions)?;
        resolve(&response, questions, &provider.model, 0.8, 0.2)?;
        Ok(Self {
            format_version: 1,
            context_sha256: context_identity(source)?,
            evaluator: provider.into(),
            questions: questions.clone(),
            response,
            observed_at_unix_ms: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map_err(|_| "System clock precedes the Unix epoch")?
                .as_millis() as u64,
        })
    }

    pub fn decide(&self, source: &Value, policy: Policy) -> Result<Decisions, &'static str> {
        if self.format_version != 1 {
            return Err("Unsupported observation format");
        }
        if self.context_sha256 != context_identity(source)? {
            return Err("Observation context does not match the supplied source");
        }
        if self.evaluator.model.is_empty()
            || self.evaluator.revision.is_empty()
            || self.evaluator.endpoint_sha256.len() != 64
            || !self
                .evaluator
                .endpoint_sha256
                .bytes()
                .all(|c| c.is_ascii_hexdigit())
        {
            return Err("Incomplete evaluator identity");
        }
        policy.validate()?;
        validate_questions(&self.questions)?;
        resolve(
            &self.response,
            &self.questions,
            &self.evaluator.model,
            policy.accept,
            policy.reject,
        )
    }
}

pub fn context_identity(source: &Value) -> Result<String, &'static str> {
    if !source.is_object() {
        return Err("Observation source must be a projected row object");
    }
    let mut hash = Sha256::new();
    hash.update(b"jev-context-v1");
    hash_value(&mut hash, source)?;
    Ok(format!("{:x}", hash.finalize()))
}

fn hash_bytes(hash: &mut Sha256, bytes: &[u8]) {
    hash.update((bytes.len() as u64).to_be_bytes());
    hash.update(bytes);
}

fn hash_value(hash: &mut Sha256, value: &Value) -> Result<(), &'static str> {
    match value {
        Value::Null => hash.update(b"n"),
        Value::Bool(value) => hash.update(if *value { b"t" } else { b"f" }),
        Value::String(value) => {
            hash.update(b"s");
            hash_bytes(hash, value.as_bytes());
        }
        Value::Number(value) => {
            hash.update(b"d");
            hash_bytes(hash, canonical_number(&value.to_string())?.as_bytes());
        }
        Value::Array(items) => {
            hash.update(b"a");
            hash.update((items.len() as u64).to_be_bytes());
            for item in items {
                hash_value(hash, item)?;
            }
        }
        Value::Object(fields) => {
            hash.update(b"o");
            hash.update((fields.len() as u64).to_be_bytes());
            let mut fields: Vec<_> = fields.iter().collect();
            fields.sort_unstable_by_key(|(key, _)| *key);
            for (key, value) in fields {
                hash_bytes(hash, key.as_bytes());
                hash_value(hash, value)?;
            }
        }
    }
    Ok(())
}

fn canonical_number(number: &str) -> Result<String, &'static str> {
    // Normalize decimal spelling exactly, without converting through floating point.
    let (mantissa, exponent) = number.split_once(['e', 'E']).unwrap_or((number, "0"));
    let exponent: i64 = exponent
        .parse()
        .map_err(|_| "Numeric exponent exceeds identity limits")?;
    let fraction = mantissa
        .split_once('.')
        .map_or(0, |(_, digits)| digits.len());
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let significant = digits.trim_start_matches('0');
    if significant.is_empty() {
        return Ok("0".into());
    }
    let coefficient = significant.trim_end_matches('0');
    let scale = exponent
        .checked_sub(fraction as i64)
        .and_then(|value| value.checked_add((significant.len() - coefficient.len()) as i64))
        .ok_or("Numeric exponent exceeds identity limits")?;
    let sign = if number.starts_with('-') { "-" } else { "" };
    Ok(format!("{sign}{coefficient}e{scale}"))
}
