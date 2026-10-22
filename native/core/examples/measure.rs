//! JSON-lines driver for measuring the production executor independently of PostgreSQL.
use jev_executor::{Executor, Limits, Provider, Questions};
use serde::Deserialize;
use serde_json::{Value, json};
use std::io::{self, BufRead};
use std::time::Instant;

#[derive(Deserialize)]
struct Run {
    provider: Provider,
    limits: Limits,
    rows: Vec<Value>,
    questions: Questions,
    #[serde(default)]
    warm: bool,
    #[serde(default)]
    map: bool,
    #[serde(default)]
    projections: jev_executor::record_map::Projections,
    #[serde(default)]
    arrivals: Vec<Value>,
}

fn main() {
    for line in io::stdin().lock().lines() {
        let run: Run = serde_json::from_str(&line.unwrap()).unwrap();
        let mut executor = Executor::new(run.provider, run.limits).unwrap();
        let mut session = jev_executor::record_map::Session::default();
        let mut passes = Vec::new();
        let mut waves = vec![&run.rows];
        if run.warm {
            waves.push(&run.rows);
        }
        if !run.arrivals.is_empty() {
            waves.push(&run.arrivals);
        }
        for rows in waves {
            let start = Instant::now();
            let mut results = Vec::new();
            let window = if run.map {
                512
            } else {
                executor.limits.batch_rows
            };
            for batch in rows.chunks(window) {
                if run.map {
                    results.extend(
                        session
                            .evaluate(
                                &mut executor,
                                batch,
                                &run.questions,
                                &run.projections,
                                || {},
                            )
                            .unwrap()
                            .decisions,
                    );
                } else {
                    results.extend(
                        executor
                            .evaluate(batch, &run.questions, || {})
                            .unwrap()
                            .into_iter()
                            .map(|result| result.decisions),
                    );
                }
            }
            passes.push(json!({
                "seconds": start.elapsed().as_secs_f64(),
                "usage_cumulative": executor.usage,
                "decisions": results,
            }));
        }
        println!("{}", json!({"passes": passes}));
    }
}
