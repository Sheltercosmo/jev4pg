use jev_executor::record_map::{Projections, Session};
use jev_executor::{Decision, Executor, Limits, Provider, Questions};
use serde_json::{Value, json};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
};
use std::thread;
use std::time::Duration;

struct Fixture {
    endpoint: String,
    requests: Arc<Mutex<Vec<Value>>>,
    stop: Arc<AtomicBool>,
    worker: Option<thread::JoinHandle<()>>,
}

impl Fixture {
    fn new(fail_first: bool) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let endpoint = format!("http://{}/v1/systemone", listener.local_addr().unwrap());
        let requests = Arc::new(Mutex::new(Vec::new()));
        let recorded = requests.clone();
        let stop = Arc::new(AtomicBool::new(false));
        let done = stop.clone();
        let worker = thread::spawn(move || {
            while !done.load(Ordering::SeqCst) {
                let Ok((mut stream, _)) = listener.accept() else {
                    thread::sleep(Duration::from_millis(1));
                    continue;
                };
                stream.set_nonblocking(false).unwrap();
                stream
                    .set_read_timeout(Some(Duration::from_secs(5)))
                    .unwrap();
                let mut reader = BufReader::new(stream.try_clone().unwrap());
                let mut length = 0;
                loop {
                    let mut line = String::new();
                    reader.read_line(&mut line).unwrap();
                    if line == "\r\n" {
                        break;
                    }
                    if let Some(value) = line.to_lowercase().strip_prefix("content-length:") {
                        length = value.trim().parse().unwrap();
                    }
                }
                let mut body = vec![0; length];
                reader.read_exact(&mut body).unwrap();
                let request: Value = serde_json::from_slice(&body).unwrap();
                let mut calls = recorded.lock().unwrap();
                let fail = fail_first && calls.is_empty();
                calls.push(request.clone());
                drop(calls);
                let answers: serde_json::Map<_, _> = request["questions"].as_object().unwrap().iter().map(|(id, question)| {
                    let record = &question["instructions"]["record"];
                    let p = if record.as_object().unwrap().values().any(|v| v == "ambiguous") { 0.5 } else { 0.9 };
                    let answer = match question["type"].as_str().unwrap() {
                        "choice" => json!({"type":"choice", "choice":"a", "probabilities":{"a":0.9,"b":0.1}}),
                        "score" => json!({"type":"score", "score":0.9, "probabilities":{"0":0.1,"1":0.9}, "legend":{"0":"Low","1":"High"}}),
                        _ => json!({"type":"noul", "noul":p}),
                    };
                    (id.clone(), answer)
                }).collect();
                let output = json!({"model": request["model"], "answers":answers}).to_string();
                let status = if fail { "500 Failed" } else { "200 OK" };
                write!(
                    stream,
                    "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{output}",
                    output.len()
                )
                .unwrap();
            }
        });
        Self {
            endpoint,
            requests,
            stop,
            worker: Some(worker),
        }
    }

    fn executor(&self, max_requests: usize) -> Executor {
        Executor::new(
            Provider {
                endpoint: self.endpoint.clone(),
                model: "fixture".into(),
                revision: "1".into(),
                api_key: String::new(),
            },
            Limits {
                max_requests,
                max_judgments: 10_000,
                ..Limits::default()
            },
        )
        .unwrap()
    }

    fn count(&self) -> usize {
        self.requests.lock().unwrap().len()
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        self.worker.take().unwrap().join().unwrap();
    }
}

fn question() -> Questions {
    serde_json::from_value(json!({"q":{"type":"noul", "instructions":"Does this record qualify?", "subject_column":"value"}})).unwrap()
}

#[test]
fn continuous_inputs_reuse_exact_values_and_replay_policy_without_provider_work() {
    let server = Fixture::new(false);
    let mut executor = server.executor(100);
    let mut cache = Session::default();
    let questions = question();
    let projection = serde_json::from_value(json!({"q":["value"]})).unwrap();
    let rows = vec![
        json!({"value":"完成", "id":1}),
        json!({"value":"完成", "id":2}),
        json!({"value":"ambiguous", "id":3}),
        json!({"value":null, "id":4}),
    ];
    let first = cache
        .evaluate(&mut executor, &rows, &questions, &projection, || {})
        .unwrap();
    assert_eq!(first.unique_judgments, 2);
    assert_eq!(server.count(), 1);
    assert_eq!(first.decisions[0], first.decisions[1]);
    assert!(matches!(first.decisions[2]["q"], Decision::Unknown { .. }));
    assert!(matches!(
        first.decisions[3]["q"],
        Decision::NotEvaluated { .. }
    ));
    assert_eq!(first.bindings.len(), 3);
    assert_eq!(first.observations[0].questions.len(), 2);
    let next = vec![
        json!({"value":"完成", "id":9}),
        json!({"value":"新值", "id":10}),
    ];
    let second = cache
        .evaluate(&mut executor, &next, &questions, &projection, || {})
        .unwrap();
    assert_eq!(second.unique_judgments, 1);
    assert_eq!(second.reused_judgments, 1);
    assert_eq!(server.count(), 2);
    executor.limits.accept = 0.95;
    let revised = cache
        .evaluate(&mut executor, &next, &questions, &projection, || {})
        .unwrap();
    assert_eq!(revised.reused_judgments, 2);
    assert!(
        revised
            .decisions
            .iter()
            .all(|row| matches!(row["q"], Decision::Unknown { .. }))
    );
    assert_eq!(server.count(), 2);
    let mut changed = questions.clone();
    changed.get_mut("q").unwrap().instructions =
        json!("Does the record meet the revised objective?");
    cache
        .evaluate(&mut executor, &next, &changed, &projection, || {})
        .unwrap();
    assert_eq!(server.count(), 3);
    let mut other_provider = server.executor(100);
    // A different provider revision has a different semantic identity.
    other_provider = Executor::new(
        Provider {
            endpoint: server.endpoint.clone(),
            model: "fixture".into(),
            revision: "2".into(),
            api_key: String::new(),
        },
        other_provider.limits.clone(),
    )
    .unwrap();
    cache
        .evaluate(&mut other_provider, &next, &questions, &projection, || {})
        .unwrap();
    assert_eq!(server.count(), 4);
}

#[test]
fn independent_columns_form_dictionaries_not_a_cartesian_product() {
    let server = Fixture::new(false);
    let mut executor = server.executor(100);
    let mut cache = Session::default();
    let questions = serde_json::from_value(json!({
        "left":{"type":"noul","instructions":"Check the left feature."},
        "right":{"type":"noul","instructions":"Check the right feature."}
    }))
    .unwrap();
    let projections = serde_json::from_value(json!({"left":["a"],"right":["b"]})).unwrap();
    let rows: Vec<_> = (0..120).map(|i| json!({"id":i,"a":i%3,"b":i%2})).collect();
    let result = cache
        .evaluate(&mut executor, &rows, &questions, &projections, || {})
        .unwrap();
    assert_eq!(result.unique_judgments, 5);
    assert_eq!(result.decisions.len(), 120);
    assert_eq!(result.bindings.len(), 240);
    assert_eq!(server.count(), 1);
    for binding in &result.bindings {
        let evidence = &result.observations[binding.observation];
        assert_eq!(
            evidence.questions[&binding.provider_question].instructions["record"]
                .as_object()
                .unwrap()
                .len(),
            1
        );
    }
    assert!(
        cache
            .evaluate(
                &mut executor,
                &[json!({"a":1})],
                &questions,
                &projections,
                || {}
            )
            .is_err()
    );
    assert_eq!(server.count(), 1);
}

#[test]
fn failed_and_budget_blocked_work_is_not_cached_as_false() {
    let server = Fixture::new(true);
    let mut executor = server.executor(100);
    let mut cache = Session::default();
    let rows = vec![json!({"value":"x"})];
    let first = cache
        .evaluate(
            &mut executor,
            &rows,
            &question(),
            &Projections::new(),
            || {},
        )
        .unwrap();
    assert!(
        matches!(&first.decisions[0]["q"], Decision::NotEvaluated {operation_state,..} if operation_state=="FAILED")
    );
    assert!(first.observations.is_empty());
    let second = cache
        .evaluate(
            &mut executor,
            &rows,
            &question(),
            &Projections::new(),
            || {},
        )
        .unwrap();
    assert_eq!(second.reused_judgments, 0);
    assert_eq!(server.count(), 2);
    let mut blocked = server.executor(0);
    cache.clear();
    let result = cache
        .evaluate(&mut blocked, &rows, &question(), &Projections::new(), || {})
        .unwrap();
    assert!(
        matches!(&result.decisions[0]["q"], Decision::NotEvaluated {operation_state,..} if operation_state=="BLOCKED_BY_BUDGET")
    );
    assert_eq!(server.count(), 2);
}

#[test]
fn expiration_and_capacity_apply_even_when_a_batch_repeats() {
    let server = Fixture::new(false);
    let mut executor = server.executor(100);
    let mut cache = Session::new(8_000_000, Duration::from_millis(10)).unwrap();
    let rows = vec![json!({"value":1})];
    cache
        .evaluate(
            &mut executor,
            &rows,
            &question(),
            &Projections::new(),
            || {},
        )
        .unwrap();
    thread::sleep(Duration::from_millis(20));
    cache
        .evaluate(
            &mut executor,
            &rows,
            &question(),
            &Projections::new(),
            || {},
        )
        .unwrap();
    assert_eq!(server.count(), 2);
    let mut disabled = Session::new(0, Duration::from_secs(10)).unwrap();
    for _ in 0..2 {
        disabled
            .evaluate(
                &mut executor,
                &rows,
                &question(),
                &Projections::new(),
                || {},
            )
            .unwrap();
    }
    assert_eq!(server.count(), 4);
}

#[test]
fn exact_numeric_equivalence_does_not_merge_different_types_or_large_integers() {
    let server = Fixture::new(false);
    let mut executor = server.executor(100);
    let mut cache = Session::default();
    let rows = [serde_json::from_str(r#"{"value":1.0}"#).unwrap()];
    cache
        .evaluate(
            &mut executor,
            &rows,
            &question(),
            &Projections::new(),
            || {},
        )
        .unwrap();
    let same = cache
        .evaluate(
            &mut executor,
            &[json!({"value":1})],
            &question(),
            &Projections::new(),
            || {},
        )
        .unwrap();
    assert_eq!(same.reused_judgments, 1);
    let distinct = vec![
        json!({"value":"1"}),
        json!({"value":true}),
        json!({"value":9007199254740992_u64}),
        json!({"value":9007199254740993_u64}),
    ];
    let result = cache
        .evaluate(
            &mut executor,
            &distinct,
            &question(),
            &Projections::new(),
            || {},
        )
        .unwrap();
    assert_eq!(result.unique_judgments, 4);
    assert_eq!(server.count(), 2);
}

#[test]
fn mixed_primitives_share_requests_and_keep_candidate_definitions() {
    let server = Fixture::new(false);
    let mut executor = server.executor(100);
    let mut cache = Session::default();
    let questions = serde_json::from_value(json!({
        "choose":{"type":"choice","instructions":"Select a candidate.","criteria":{"a":"First candidate","b":"No applicable candidate"}},
        "rate":{"type":"score","instructions":"Rate.","criteria":["Low","High"]}
    })).unwrap();
    let rows: Vec<_> = (0..40).map(|i| json!({"value":i})).collect();
    let result = cache
        .evaluate(&mut executor, &rows, &questions, &Projections::new(), || {})
        .unwrap();
    assert_eq!(result.unique_judgments, 80);
    assert_eq!(server.count(), 3);
    assert!(result.decisions.iter().all(|row| row.len() == 2));
    assert!(
        server
            .requests
            .lock()
            .unwrap()
            .iter()
            .all(|request| request["questions"].as_object().unwrap().len() <= 32)
    );
}

#[test]
fn adding_or_reordering_questions_reuses_their_independent_evidence() {
    let server = Fixture::new(false);
    let mut executor = server.executor(100);
    let mut cache = Session::default();
    let rows = vec![json!({"value":"完成"})];
    cache
        .evaluate(
            &mut executor,
            &rows,
            &question(),
            &Projections::new(),
            || {},
        )
        .unwrap();
    let mut changed = question();
    let original = changed.remove("q").unwrap();
    changed.insert("earlier".into(), original);
    let mut added = changed["earlier"].clone();
    added.instructions = json!("Is the record urgent?");
    changed.insert("additional".into(), added);
    let result = cache
        .evaluate(&mut executor, &rows, &changed, &Projections::new(), || {})
        .unwrap();
    assert_eq!((result.unique_judgments, result.reused_judgments), (1, 1));
    assert_eq!(result.observations.len(), 2);
    assert_eq!(server.count(), 2);
    assert_eq!(result.decisions[0].len(), 2);
}

#[test]
fn replay_checks_current_policy_and_changed_candidates_across_shared_batches() {
    let server = Fixture::new(false);
    let mut executor = server.executor(100);
    let mut cache = Session::default();
    let questions: Questions = serde_json::from_value(json!({
        "choose":{"type":"choice","instructions":"Select.","criteria":{"a":"First","b":"None"}},
        "rate":{"type":"score","instructions":"Rate.","criteria":["Low","High"]}
    }))
    .unwrap();
    let rows: Vec<_> = (0..40).map(|i| json!({"value":i})).collect();
    cache
        .evaluate(&mut executor, &rows, &questions, &Projections::new(), || {})
        .unwrap();
    executor.limits.choice_min = 0.95;
    executor.limits.score_confidence_min = 0.5;
    let replay = cache
        .evaluate(&mut executor, &rows, &questions, &Projections::new(), || {})
        .unwrap();
    assert_eq!(server.count(), 3);
    assert_eq!(replay.reused_judgments, 80);
    assert!(
        replay
            .decisions
            .iter()
            .flat_map(|row| row.values())
            .all(|d| matches!(d, Decision::Unknown { .. }))
    );
    let mut changed = questions;
    changed.get_mut("choose").unwrap().criteria = Some(json!({"a":"Revised candidate","b":"None"}));
    let revised = cache
        .evaluate(&mut executor, &rows, &changed, &Projections::new(), || {})
        .unwrap();
    assert_eq!(server.count(), 5);
    assert_eq!(
        (revised.unique_judgments, revised.reused_judgments),
        (40, 40)
    );
    assert_eq!(revised.decisions.len(), 40);
}

#[test]
fn bounded_cache_evicts_old_batches_without_discarding_recently_used_values() {
    let server = Fixture::new(false);
    let mut executor = server.executor(100);
    let mut cache = Session::default();
    let first = cache
        .evaluate(
            &mut executor,
            &[json!({"value":"x"})],
            &question(),
            &Projections::new(),
            || {},
        )
        .unwrap();
    let bytes = serde_json::to_vec(first.observations[0].as_ref())
        .unwrap()
        .len()
        + 129
        + 128;
    let mut cache = Session::new(bytes * 2 + 8, Duration::from_secs(300)).unwrap();
    for value in ["x", "y", "x", "z", "x"] {
        cache
            .evaluate(
                &mut executor,
                &[json!({"value":value})],
                &question(),
                &Projections::new(),
                || {},
            )
            .unwrap();
    }
    assert_eq!(server.count(), 4);
    let evicted = cache
        .evaluate(
            &mut executor,
            &[json!({"value":"y"})],
            &question(),
            &Projections::new(),
            || {},
        )
        .unwrap();
    assert_eq!(evicted.unique_judgments, 1);
    assert_eq!(server.count(), 5);
}

#[test]
fn large_independent_frontiers_keep_one_budget_and_all_source_rows() {
    let server = Fixture::new(false);
    let mut executor = server.executor(2);
    let mut cache = Session::default();
    let mut questions = question();
    for id in ["second", "third"] {
        let mut q = questions["q"].clone();
        q.instructions = json!(format!("Evaluate {id} objective."));
        questions.insert(id.into(), q);
    }
    let rows: Vec<_> = (0..512).map(|i| json!({"value":i})).collect();
    let result = cache
        .evaluate(&mut executor, &rows, &questions, &Projections::new(), || {})
        .unwrap();
    assert_eq!(result.unique_judgments, 1536);
    assert_eq!(result.decisions.len(), 512);
    assert_eq!(server.count(), 2);
    let values = result
        .decisions
        .iter()
        .flat_map(|row| row.values())
        .filter(|d| matches!(d, Decision::Value { .. }))
        .count();
    assert_eq!(values, 64);
    assert_eq!(executor.usage.requests, 2);
    assert_eq!(executor.usage.judgments, 64);
    assert!(result.decisions.iter().flat_map(|row| row.values()).all(|d| matches!(d, Decision::Value {..}) || matches!(d, Decision::NotEvaluated {operation_state,..} if operation_state == "BLOCKED_BY_BUDGET")));
}
