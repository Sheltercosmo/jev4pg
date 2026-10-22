//! Exact context dictionaries for continuous input. No similarity-based answer reuse.

use crate::evidence::{Observation, context_identity, request_identity};
use crate::{
    Decision, Decisions, EvaluationInput, Executor, Question, Questions, Receipt,
    applicable_questions, resolve, validate_questions,
};
use serde::Serialize;
use serde_json::{Value, json};
use std::collections::{BTreeMap, HashMap, VecDeque};
use std::sync::Arc;
use std::time::{Duration, Instant};

pub type Projections = BTreeMap<String, Vec<String>>;

struct Cached {
    observation: Arc<Observation>,
    question: String,
    inserted: Instant,
    receipt: Option<Receipt>,
}

struct CachedBatch {
    observation: Arc<Observation>,
    identities: Vec<String>,
    bytes: usize,
}

struct Work {
    question: Question,
    targets: Vec<(usize, String)>,
    identity: String,
}

struct Replayed {
    decision: Decision,
    observation: usize,
    question: String,
    receipt: Option<Receipt>,
}

#[derive(Serialize)]
pub struct EvidenceBinding {
    pub ordinal: usize,
    pub question: String,
    pub observation: usize,
    pub provider_question: String,
    pub reused: bool,
}

pub struct MappedResult {
    pub decisions: Vec<Decisions>,
    pub observations: Vec<Arc<Observation>>,
    pub bindings: Vec<EvidenceBinding>,
    pub receipts: Vec<MappedReceipt>,
    pub reused_judgments: usize,
    pub unique_judgments: usize,
}

#[derive(Clone, Serialize)]
pub struct MappedReceipt {
    pub ordinal: usize,
    pub question: String,
    pub receipt: Receipt,
}

pub struct Session {
    cache: HashMap<String, Cached>,
    batches: VecDeque<CachedBatch>,
    bytes: usize,
    capacity: usize,
    max_age: Duration,
}

impl Default for Session {
    fn default() -> Self {
        Self::new(8_000_000, Duration::from_secs(300)).expect("Default cache bounds are valid")
    }
}

impl Session {
    pub fn new(capacity: usize, max_age: Duration) -> Result<Self, &'static str> {
        if capacity > 8_000_000 || max_age > Duration::from_secs(86_400) {
            return Err("Record cache is limited to 8 MB and one day");
        }
        Ok(Self {
            cache: HashMap::new(),
            batches: VecDeque::new(),
            bytes: 0,
            capacity,
            max_age,
        })
    }

    pub fn clear(&mut self) {
        self.cache.clear();
        self.batches.clear();
        self.bytes = 0;
    }

    pub fn evaluate(
        &mut self,
        executor: &mut Executor,
        rows: &[Value],
        questions: &Questions,
        projections: &Projections,
        mut check_interrupt: impl FnMut(),
    ) -> Result<MappedResult, &'static str> {
        validate_questions(questions)?;
        executor.limits.validate()?;
        // This session owns row-level reuse and freshness; do not bypass it via batch reuse.
        executor.clear_memory_cache();
        if rows.len() > 512
            || serde_json::to_vec(rows)
                .map_err(|_| "Invalid source")?
                .len()
                > 8_000_000
        {
            return Err("Map at most 512 records and 8 MB per window");
        }
        for (id, columns) in projections {
            if !questions.contains_key(id) || columns.is_empty() || columns.len() > 128 {
                return Err("Projection must declare existing questions and required columns");
            }
            if questions[id]
                .subject_column
                .as_ref()
                .is_some_and(|subject| !columns.contains(subject))
            {
                return Err("Projection must retain the subject column");
            }
        }
        let mut output = MappedResult {
            decisions: Vec::new(),
            observations: Vec::new(),
            bindings: Vec::new(),
            receipts: Vec::new(),
            reused_judgments: 0,
            unique_judgments: 0,
        };
        let mut observed = HashMap::new();
        let mut resolved: HashMap<usize, Decisions> = HashMap::new();
        let mut missing: Vec<Work> = Vec::new();
        let mut unique = HashMap::new();
        let mut replayed: HashMap<String, Replayed> = HashMap::new();
        let mut prepared_bytes = 0;
        let definitions: BTreeMap<_, _> = questions
            .iter()
            .map(|(id, question)| {
                let mut definition = question.clone();
                definition.subject_column = None;
                request_identity(
                    &json!({"record_map_format": 2}),
                    &BTreeMap::from([("mapped".into(), definition)]),
                    &executor.provider,
                )
                .map(|identity| (id.clone(), identity))
            })
            .collect::<Result<_, _>>()?;
        for (ordinal, row) in rows.iter().enumerate() {
            check_interrupt();
            let (active, skipped) = applicable_questions(row, questions)?;
            output.decisions.push(skipped);
            for (id, question) in active {
                let context = project(row, projections.get(&id))?;
                let identity = format!("{}:{}", definitions[&id], context_identity(&context)?);
                if let Some(saved) = replayed.get(&identity) {
                    if let Some(receipt) = &saved.receipt {
                        output.receipts.push(MappedReceipt {
                            ordinal,
                            question: id.clone(),
                            receipt: receipt.clone(),
                        });
                    }
                    output.decisions[ordinal].insert(id.clone(), saved.decision.clone());
                    output.bindings.push(EvidenceBinding {
                        ordinal,
                        question: id,
                        observation: saved.observation,
                        provider_question: saved.question.clone(),
                        reused: true,
                    });
                    output.reused_judgments += 1;
                } else if let Some(cached) = self
                    .cache
                    .get(&identity)
                    .filter(|saved| saved.inserted.elapsed() < self.max_age)
                {
                    let pointer = Arc::as_ptr(&cached.observation) as usize;
                    // The key binds context, question and evaluator. Replay each authentic batch once.
                    let decisions = match resolved.entry(pointer) {
                        std::collections::hash_map::Entry::Occupied(entry) => entry.into_mut(),
                        std::collections::hash_map::Entry::Vacant(entry) => entry.insert(resolve(
                            &cached.observation.response,
                            &cached.observation.questions,
                            &cached.observation.evaluator.model,
                            &executor.limits.policy(),
                        )?),
                    };
                    let decision = decisions
                        .get(&cached.question)
                        .ok_or("Cached question is missing")?
                        .clone();
                    let observation = *observed.entry(pointer).or_insert_with(|| {
                        output.observations.push(cached.observation.clone());
                        output.observations.len() - 1
                    });
                    replayed.insert(
                        identity,
                        Replayed {
                            decision: decision.clone(),
                            observation,
                            question: cached.question.clone(),
                            receipt: cached.receipt.clone(),
                        },
                    );
                    if let Some(receipt) = &cached.receipt {
                        output.receipts.push(MappedReceipt {
                            ordinal,
                            question: id.clone(),
                            receipt: receipt.clone(),
                        });
                    }
                    output.decisions[ordinal].insert(id.clone(), decision);
                    output.bindings.push(EvidenceBinding {
                        ordinal,
                        question: id,
                        observation,
                        provider_question: cached.question.clone(),
                        reused: true,
                    });
                    output.reused_judgments += 1;
                    self.touch(pointer);
                } else if let Some(&index) = unique.get(&identity) {
                    let work: &mut Work = &mut missing[index];
                    work.targets.push((ordinal, id));
                } else {
                    let question = Question {
                        instructions: json!({"record": context, "question": question.instructions}),
                        subject_column: None,
                        ..question
                    };
                    prepared_bytes += serde_json::to_vec(&question)
                        .map_err(|_| "Invalid mapped question")?
                        .len();
                    if prepared_bytes > 8_000_000 {
                        return Err("Prepared map window exceeds 8 MB; reduce the source window");
                    }
                    unique.insert(identity.clone(), missing.len());
                    missing.push(Work {
                        question,
                        targets: vec![(ordinal, id)],
                        identity,
                    });
                }
            }
        }
        output.unique_judgments = missing.len();
        let mut batches: Vec<(Questions, Vec<Work>)> = Vec::new();
        let mut packed = Questions::new();
        let mut members = Vec::new();
        let mut packed_bytes = 2;
        for work in missing {
            let question_bytes = serde_json::to_vec(&work.question)
                .map_err(|_| "Invalid map question")?
                .len();
            if question_bytes > 31_000 {
                return Err("One mapped question exceeds the context limit");
            }
            if packed.len() == 32 || packed_bytes + question_bytes + 8 > 63_000 {
                batches.push((std::mem::take(&mut packed), std::mem::take(&mut members)));
                packed_bytes = 2;
            }
            packed.insert(format!("q{}", members.len()), work.question.clone());
            packed_bytes += question_bytes + 8;
            members.push(work);
        }
        if !members.is_empty() {
            batches.push((packed, members));
        }
        let state = json!({});
        for window in batches.chunks(512) {
            let inputs: Vec<_> = window
                .iter()
                .map(|(questions, _)| EvaluationInput {
                    source: &state,
                    questions,
                })
                .collect();
            let evaluations = executor.evaluate_frontier(&inputs, &mut check_interrupt)?;
            for ((packed, members), evaluation) in window.iter().zip(evaluations) {
                let observation = evaluation.observation.map(Arc::new);
                let mut saved_index = None;
                if let Some(observation) = &observation {
                    saved_index = Some(output.observations.len());
                    output.observations.push(observation.clone());
                    self.remember(observation.clone(), members, evaluation.receipt.clone());
                }
                for (i, work) in members.iter().enumerate() {
                    let key = format!("q{i}");
                    debug_assert!(packed.contains_key(&key));
                    for (ordinal, id) in &work.targets {
                        if let Some(receipt) = &evaluation.receipt {
                            output.receipts.push(MappedReceipt {
                                ordinal: *ordinal,
                                question: id.clone(),
                                receipt: receipt.clone(),
                            });
                        }
                        output.decisions[*ordinal]
                            .insert(id.clone(), evaluation.decisions[&key].clone());
                        if let Some(observation) = saved_index {
                            output.bindings.push(EvidenceBinding {
                                ordinal: *ordinal,
                                question: id.clone(),
                                observation,
                                provider_question: key.clone(),
                                reused: false,
                            });
                        }
                    }
                }
            }
        }
        if serde_json::to_vec(&(&output.decisions, &output.receipts, &output.bindings))
            .expect("Decisions serialize")
            .len()
            > 8_000_000
        {
            return Err("Mapped decisions exceed 8 MB");
        }
        let evidence_bytes = output
            .observations
            .iter()
            .map(|observation| {
                serde_json::to_vec(observation.as_ref())
                    .expect("Observation serializes")
                    .len()
            })
            .sum::<usize>();
        if evidence_bytes > 8_000_000 {
            return Err("Mapped evidence exceeds 8 MB");
        }
        Ok(output)
    }

    fn touch(&mut self, pointer: usize) {
        if let Some(index) = self
            .batches
            .iter()
            .position(|batch| Arc::as_ptr(&batch.observation) as usize == pointer)
        {
            let batch = self.batches.remove(index).expect("Batch exists");
            self.batches.push_back(batch);
        }
    }

    fn remember(
        &mut self,
        observation: Arc<Observation>,
        members: &[Work],
        receipt: Option<Receipt>,
    ) {
        let receipt_bytes = receipt.as_ref().map_or(0, |receipt| {
            serde_json::to_vec(receipt)
                .expect("Receipt serializes")
                .len()
        });
        let bytes = serde_json::to_vec(observation.as_ref())
            .expect("Observation serializes")
            .len()
            + members
                .iter()
                .map(|work| work.identity.len() + 128 + receipt_bytes)
                .sum::<usize>();
        if bytes > self.capacity || self.max_age.is_zero() {
            return;
        }
        while self.bytes + bytes > self.capacity {
            let evicted = self
                .batches
                .pop_front()
                .expect("Accounted cache batch exists");
            self.bytes -= evicted.bytes;
            for id in &evicted.identities {
                if self
                    .cache
                    .get(id)
                    .is_some_and(|entry| Arc::ptr_eq(&entry.observation, &evicted.observation))
                {
                    self.cache.remove(id);
                }
            }
        }
        let mut identities = Vec::new();
        for (i, work) in members.iter().enumerate() {
            identities.push(work.identity.clone());
            self.cache.insert(
                work.identity.clone(),
                Cached {
                    observation: observation.clone(),
                    question: format!("q{i}"),
                    inserted: Instant::now(),
                    receipt: receipt.clone(),
                },
            );
        }
        self.bytes += bytes;
        self.batches.push_back(CachedBatch {
            observation,
            identities,
            bytes,
        });
    }
}

fn project(row: &Value, columns: Option<&Vec<String>>) -> Result<Value, &'static str> {
    let Some(columns) = columns else {
        return Ok(row.clone());
    };
    let mut selected = serde_json::Map::new();
    for column in columns {
        selected.insert(
            column.clone(),
            row.get(column)
                .ok_or("Required context column is absent")?
                .clone(),
        );
    }
    Ok(Value::Object(selected))
}
