use crate::{Questions, validate_questions};
use serde::Deserialize;
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Plan {
    pub version: u32,
    pub target: String,
    pub stages: Vec<Stage>,
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Stage {
    pub id: String,
    pub operator: String,
    pub sql: String,
    pub columns: BTreeMap<String, Column>,
    #[serde(default)]
    pub inputs: Vec<Input>,
    #[serde(default)]
    pub questions: Questions,
    #[serde(default)]
    pub guard: Option<Guard>,
    #[serde(default)]
    pub grain: Option<Vec<String>>,
    #[serde(default)]
    pub keys: Vec<Vec<String>>,
    #[serde(default)]
    pub assertions: Vec<String>,
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Column {
    pub kind: String,
    pub label: String,
    #[serde(default = "nullable")]
    pub nullable: bool,
    #[serde(default)]
    pub unit: Option<String>,
    #[serde(default)]
    pub lineage: Vec<String>,
}

fn nullable() -> bool {
    true
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Input {
    pub stage: String,
    pub alias: String,
    #[serde(default)]
    pub require_values: Option<Vec<String>>,
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Guard {
    pub input: String,
    pub question: String,
    pub equals: Value,
}

pub fn identifier(value: &str) -> bool {
    !value.is_empty() && value.len() <= 63 && !value.contains('\0') && !value.starts_with("__jev_")
}

pub fn same_scalar(left: &Value, right: &Value) -> Result<bool, &'static str> {
    match (left, right) {
        (Value::Number(left), Value::Number(right)) => {
            Ok(crate::evidence::canonical_number(&left.to_string())?
                == crate::evidence::canonical_number(&right.to_string())?)
        }
        _ => Ok(left == right),
    }
}

impl Plan {
    pub fn parse(value: Value) -> Result<Self, &'static str> {
        if serde_json::to_vec(&value)
            .map_err(|_| "Invalid plan")?
            .len()
            > 2_000_000
        {
            return Err("Native plans must not exceed 2 MB");
        }
        let plan: Self = serde_json::from_value(value).map_err(|_| "Invalid typed native plan")?;
        plan.validate()?;
        Ok(plan)
    }

    pub fn validate(&self) -> Result<(), &'static str> {
        if self.version != 1 || !(1..=32).contains(&self.stages.len()) {
            return Err("Supply a version 1 plan with 1 to 32 stages");
        }
        let stages: BTreeMap<_, _> = self.stages.iter().map(|s| (s.id.as_str(), s)).collect();
        if stages.len() != self.stages.len() || !stages.contains_key(self.target.as_str()) {
            return Err("Stage identities must be unique and the target must exist");
        }
        for stage in &self.stages {
            if !identifier(&stage.id)
                || stage.sql.is_empty()
                || stage.sql.len() > 30_000
                || stage.columns.is_empty()
                || ![
                    "source",
                    "filter",
                    "project",
                    "aggregate",
                    "window",
                    "join",
                    "semantic",
                    "row_identity",
                    "latest",
                    "order_limit",
                ]
                .contains(&stage.operator.as_str())
            {
                return Err("Invalid stage identity, operator, SQL or output schema");
            }
            for (name, column) in &stage.columns {
                if !identifier(name)
                    || ![
                        "integer", "number", "text", "boolean", "date", "datetime", "json", "other",
                    ]
                    .contains(&column.kind.as_str())
                {
                    return Err("Invalid typed output column");
                }
            }
            if stage.operator == "semantic" {
                validate_questions(&stage.questions)?;
            } else if !stage.questions.is_empty() {
                return Err("Only semantic stages declare questions");
            }
            if stage
                .keys
                .iter()
                .any(|key| key.iter().any(|name| !stage.columns.contains_key(name)))
                || stage
                    .grain
                    .as_ref()
                    .is_some_and(|grain| grain.iter().any(|name| !stage.columns.contains_key(name)))
            {
                return Err("Stage grain and keys must name output columns");
            }
            let mut aliases = BTreeSet::new();
            for input in &stage.inputs {
                let parent = stages
                    .get(input.stage.as_str())
                    .ok_or("Missing stage dependency")?;
                if !identifier(&input.alias) || !aliases.insert(&input.alias) {
                    return Err("Input aliases must be valid and unique");
                }
                if input
                    .require_values
                    .as_ref()
                    .is_some_and(|ids| ids.iter().any(|id| !parent.questions.contains_key(id)))
                {
                    return Err("A completeness requirement names an undeclared decision");
                }
            }
            if let Some(guard) = &stage.guard {
                let input = stage
                    .inputs
                    .iter()
                    .find(|i| i.alias == guard.input)
                    .ok_or("Guard input is absent")?;
                if !stages[input.stage.as_str()]
                    .questions
                    .contains_key(&guard.question)
                    || !matches!(
                        guard.equals,
                        Value::Bool(_) | Value::String(_) | Value::Number(_)
                    )
                {
                    return Err("A guard requires a declared decision and a scalar comparison");
                }
            }
        }
        let mut closed = BTreeSet::new();
        while closed.len() < stages.len() {
            let before = closed.len();
            for stage in &self.stages {
                if stage
                    .inputs
                    .iter()
                    .all(|input| closed.contains(input.stage.as_str()))
                {
                    closed.insert(stage.id.as_str());
                }
            }
            if closed.len() == before {
                return Err("Native stage dependencies contain a cycle");
            }
        }
        let mut reachable = BTreeSet::new();
        let mut pending = vec![self.target.as_str()];
        while let Some(id) = pending.pop() {
            if reachable.insert(id) {
                pending.extend(stages[id].inputs.iter().map(|input| input.stage.as_str()));
            }
        }
        if reachable.len() != stages.len() {
            return Err("Every stage must contribute to the target");
        }
        Ok(())
    }
}
