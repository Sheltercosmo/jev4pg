use jev_executor::{Decision, Executor, Limits, Provider};
use serde_json::{Value, json};
use std::{
    io::{BufRead, BufReader, Read, Write},
    net::TcpListener,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    thread,
    time::Duration,
};

#[test]
fn parallel_batches_reuse_context_and_enforce_one_query_budget() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let endpoint = format!("http://{}/v1/systemone", listener.local_addr().unwrap());
    let active = Arc::new(AtomicUsize::new(0));
    let peak = Arc::new(AtomicUsize::new(0));
    let observed_peak = peak.clone();
    let server = thread::spawn(move || {
        let mut threads = Vec::new();
        for stream in listener.incoming().take(3) {
            let active = active.clone();
            let peak = peak.clone();
            threads.push(thread::spawn(move || {
                let mut stream = stream.unwrap();
                let mut reader = BufReader::new(stream.try_clone().unwrap());
                let mut length = 0;
                loop {
                    let mut line = String::new(); reader.read_line(&mut line).unwrap();
                    if line == "\r\n" { break; }
                    if let Some(value) = line.to_lowercase().strip_prefix("content-length:") { length = value.trim().parse().unwrap(); }
                }
                let mut body = vec![0;length]; reader.read_exact(&mut body).unwrap();
                let request: Value = serde_json::from_slice(&body).unwrap();
                assert_eq!(request["questions"].as_object().unwrap().len(), 2);
                let count = active.fetch_add(1, Ordering::SeqCst) + 1;
                peak.fetch_max(count, Ordering::SeqCst);
                thread::sleep(Duration::from_millis(100));
                let answers = request["questions"].as_object().unwrap().keys().map(|k| (k.clone(), json!({"type":"noul","noul":0.95}))).collect::<serde_json::Map<_,_>>();
                let output = json!({"model":"fixture-v1","answers":answers}).to_string();
                write!(stream,"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",output.len(),output).unwrap();
                active.fetch_sub(1, Ordering::SeqCst);
            }));
        }
        for thread in threads {
            thread.join().unwrap();
        }
    });
    let provider = Provider {
        endpoint,
        model: "fixture-v1".into(),
        revision: "v1".into(),
        api_key: String::new(),
    };
    let limits = Limits {
        concurrency: 2,
        max_requests: 3,
        ..Limits::default()
    };
    let mut executor = Executor::new(provider, limits).unwrap();
    let questions = serde_json::from_value(json!({"en":{"type":"noul","instructions":"Complete?"},"zh":{"type":"noul","instructions":"完成了吗？"}})).unwrap();
    let rows = vec![
        json!({"text":"完成"}),
        json!({"text":"完成"}),
        json!({"text":"Done"}),
        json!({"text":"Finished"}),
    ];
    let first = executor.evaluate(&rows, &questions, || {}).unwrap();
    assert!(
        first
            .iter()
            .all(|row| row.decisions["en"].require_bool() == Ok(true))
    );
    assert!(first.iter().all(|row| row.observation.is_some()));
    assert_eq!(executor.usage.requests, 3);
    assert_eq!(executor.usage.judgments, 6);
    assert_eq!(executor.evaluate(&rows, &questions, || {}).unwrap(), first);
    assert_eq!(executor.usage.requests, 3);
    let held = executor
        .evaluate(&[json!({"text":"New"})], &questions, || {})
        .unwrap();
    assert!(
        matches!(&held[0].decisions["en"],Decision::NotEvaluated { operation_state, .. } if operation_state == "BLOCKED_BY_BUDGET")
    );
    assert!(held[0].observation.is_none());
    server.join().unwrap();
    assert_eq!(observed_peak.load(Ordering::SeqCst), 2);
}
