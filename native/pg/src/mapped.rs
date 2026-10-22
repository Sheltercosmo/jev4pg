use crate::execution::{SemanticScan, SourceSpec};
use jev_executor::Questions;
use jev_executor::record_map::{Projections, Session};
use pgrx::JsonB;
use pgrx::prelude::*;
use serde_json::{Value, json};
use std::time::Duration;

pub struct MappedScan {
    scan: SemanticScan,
    session: Session,
    questions: Questions,
    projections: Projections,
    window_rows: usize,
    window: i64,
}

impl MappedScan {
    pub fn new(
        source_sql: &str,
        questions: Value,
        projections: Value,
        mut options: Value,
        fcinfo: pg_sys::FunctionCallInfo,
    ) -> Self {
        let settings = options
            .as_object_mut()
            .unwrap_or_else(|| error!("Execution options must be an object"));
        let window_rows = setting(settings, "map_window_rows", 512, 1, 512);
        let cache_bytes = setting(settings, "map_cache_bytes", 8_000_000, 0, 8_000_000);
        let cache_age = setting(settings, "map_cache_age_seconds", 300, 0, 86_400);
        let session = Session::new(cache_bytes as usize, Duration::from_secs(cache_age))
            .unwrap_or_else(|message| error!("{}", message));
        let typed: Questions = serde_json::from_value(questions.clone())
            .unwrap_or_else(|_| error!("Invalid typed questions"));
        let projections: Projections = serde_json::from_value(projections).unwrap_or_else(|_| {
            error!("Context columns must map question identities to column lists")
        });
        let source = SourceSpec::new("mapped".into(), source_sql.into(), questions);
        let scan = SemanticScan::new(vec![source], options, fcinfo);
        Self {
            scan,
            session,
            questions: typed,
            projections,
            window_rows: window_rows as usize,
            window: 0,
        }
    }
}

impl Iterator for MappedScan {
    type Item = (i64, JsonB, JsonB, JsonB, JsonB);

    fn next(&mut self) -> Option<Self::Item> {
        pgrx::check_for_interrupts!();
        let source = self.scan.read_mapped_window(self.window_rows);
        let rows: Vec<_> = source.iter().map(|(_, row)| row.clone()).collect();
        let result = self
            .session
            .evaluate(
                &mut self.scan.executor,
                &rows,
                &self.questions,
                &self.projections,
                || {
                    pgrx::check_for_interrupts!();
                },
            )
            .unwrap_or_else(|message| error!("{}", message));
        if source.is_empty() {
            return None;
        }
        self.window += 1;
        let rows: Vec<_> = source.into_iter().zip(result.decisions).map(|((ordinal, source), decisions)| {
            json!({"ordinal": ordinal, "source": source, "decisions": decisions})
        }).collect();
        let evidence = json!({
            "format_version": 1,
            "observations": result.observations.iter().map(|observation| observation.as_ref()).collect::<Vec<_>>(),
            "bindings": result.bindings,
            "receipts": result.receipts,
        });
        let usage = json!({
            "executor": self.scan.executor.usage,
            "window": { "unique_judgments": result.unique_judgments, "reused_judgments": result.reused_judgments },
        });
        let policy =
            serde_json::to_value(self.scan.executor.limits.policy()).expect("Policy serializes");
        if serde_json::to_vec(&(&rows, &evidence, &usage, &policy))
            .expect("Mapped window serializes")
            .len()
            > 8_000_000
        {
            error!("Mapped SQL result exceeds 8 MB; reduce map_window_rows");
        }
        Some((
            self.window,
            JsonB(json!(rows)),
            JsonB(evidence),
            JsonB(usage),
            JsonB(policy),
        ))
    }
}

fn setting(
    settings: &mut serde_json::Map<String, Value>,
    name: &str,
    default: u64,
    min: u64,
    max: u64,
) -> u64 {
    settings.remove(name).map_or(default, |value| {
        value
            .as_u64()
            .filter(|value| (min..=max).contains(value))
            .unwrap_or_else(|| error!("Invalid {}", name))
    })
}
