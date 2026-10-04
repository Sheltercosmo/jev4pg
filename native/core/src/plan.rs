use crate::conditional::{RowGuard, Selection, scalar};
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
    pub row_guard: Option<RowGuard>,
    #[serde(default)]
    pub selections: BTreeMap<String, Selection>,
    #[serde(default)]
    pub context_columns: Option<Vec<String>>,
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

impl Stage {
    pub fn decision_ids(&self) -> impl Iterator<Item = &String> {
        self.questions.keys().chain(self.selections.keys())
    }
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
                    "merge",
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
            if let Some(columns) = &stage.context_columns {
                let names: BTreeSet<_> = columns.iter().collect();
                if stage.operator != "semantic"
                    || names.is_empty()
                    || names.len() != columns.len()
                    || names.iter().any(|name| !stage.columns.contains_key(*name))
                    || stage.questions.values().any(|question| {
                        question
                            .subject_column
                            .as_ref()
                            .is_some_and(|name| !names.contains(name))
                    })
                    || stage
                        .row_guard
                        .as_ref()
                        .is_some_and(|guard| names.contains(&guard.column))
                {
                    return Err(
                        "Semantic context must name distinct projected columns, include every subject and exclude routing metadata",
                    );
                }
            }
            let decision_column = |name: &String| {
                stage
                    .columns
                    .get(name)
                    .is_some_and(|column| column.kind == "json")
            };
            if let Some(guard) = &stage.row_guard
                && (stage.operator != "semantic"
                    || !decision_column(&guard.column)
                    || !scalar(&guard.equals)
                    || stage
                        .questions
                        .values()
                        .any(|question| question.subject_column.as_ref() == Some(&guard.column)))
            {
                return Err(
                    "A row guard requires a semantic stage, a JSON decision column and a scalar comparison",
                );
            }
            if stage.operator == "merge" {
                if !(1..=32).contains(&stage.selections.len()) {
                    return Err("A merge stage requires 1 to 32 decision selections");
                }
                for (id, selection) in &stage.selections {
                    if id.is_empty()
                        || id.len() > 200
                        || !decision_column(&selection.selector)
                        || !(1..=32).contains(&selection.cases.len())
                        || selection
                            .otherwise
                            .as_ref()
                            .is_some_and(|column| !decision_column(column))
                    {
                        return Err("Invalid decision selection");
                    }
                    for (index, case) in selection.cases.iter().enumerate() {
                        if !scalar(&case.equals)
                            || !decision_column(&case.column)
                            || selection.cases[..index].iter().any(|previous| {
                                same_scalar(&previous.equals, &case.equals).unwrap_or(false)
                            })
                            || case.equals.is_boolean() != selection.cases[0].equals.is_boolean()
                            || case.equals.is_string() != selection.cases[0].equals.is_string()
                            || case.equals.is_number() != selection.cases[0].equals.is_number()
                        {
                            return Err(
                                "Selection cases require distinct scalars of one type and JSON decision columns",
                            );
                        }
                    }
                }
            } else if !stage.selections.is_empty() {
                return Err("Only merge stages declare decision selections");
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
                if input.require_values.as_ref().is_some_and(|ids| {
                    ids.iter()
                        .any(|id| !parent.decision_ids().any(|declared| declared == id))
                }) {
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
                    .decision_ids()
                    .any(|id| id == &guard.question)
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
