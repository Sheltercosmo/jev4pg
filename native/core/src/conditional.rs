use crate::{Decision, plan::same_scalar};
use serde::Deserialize;
use serde_json::Value;

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RowGuard {
    pub column: String,
    pub equals: Value,
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Selection {
    pub selector: String,
    pub cases: Vec<Case>,
    #[serde(default)]
    pub otherwise: Option<String>,
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Case {
    pub equals: Value,
    pub column: String,
}

pub fn scalar(value: &Value) -> bool {
    matches!(value, Value::Bool(_) | Value::String(_) | Value::Number(_))
}

fn decision(source: &Value, column: &str) -> Result<Decision, Decision> {
    let result: Decision = serde_json::from_value(
        source.get(column).cloned().unwrap_or(Value::Null),
    )
    .map_err(|_| Decision::blocked("FAILED", "A conditional input is not a typed decision"))?;
    if matches!(&result, Decision::Value { operation_state, .. } if operation_state != "SUCCEEDED")
    {
        return Err(Decision::blocked(
            "FAILED",
            "A conditional value did not succeed",
        ));
    }
    Ok(result)
}

fn selected_value(source: &Value, column: &str) -> Result<Value, Decision> {
    match decision(source, column)? {
        Decision::Value { value, .. } if scalar(&value) => Ok(value),
        Decision::Value { .. } => Err(Decision::blocked(
            "BLOCKED_BY_POLICY",
            "A conditional selector must have a scalar value",
        )),
        _ => Err(Decision::blocked(
            "BLOCKED_BY_DEPENDENCY",
            "The conditional selector is unresolved",
        )),
    }
}

fn compare(value: &Value, expected: &Value) -> Result<bool, Decision> {
    if value.is_boolean() != expected.is_boolean()
        || value.is_string() != expected.is_string()
        || value.is_number() != expected.is_number()
    {
        return Err(Decision::blocked(
            "BLOCKED_BY_POLICY",
            "Conditional values have different types",
        ));
    }
    same_scalar(value, expected).map_err(|message| Decision::blocked("FAILED", message))
}

impl RowGuard {
    pub fn evaluate(&self, source: &Value) -> Result<bool, Decision> {
        compare(&selected_value(source, &self.column)?, &self.equals)
    }
}

impl Selection {
    pub fn evaluate(&self, source: &Value) -> Decision {
        let value = match selected_value(source, &self.selector) {
            Ok(value) => value,
            Err(blocked) => return blocked,
        };
        let mut selected = self.otherwise.as_deref();
        for case in &self.cases {
            match compare(&value, &case.equals) {
                Ok(true) => {
                    selected = Some(&case.column);
                    break;
                }
                Ok(false) => {}
                Err(blocked) => return blocked,
            }
        }
        match selected {
            Some(column) => decision(source, column).unwrap_or_else(|blocked| blocked),
            None => Decision::blocked(
                "BLOCKED_BY_POLICY",
                "No branch covers the resolved selector",
            ),
        }
    }
}
