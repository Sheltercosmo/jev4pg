use jev_executor::{
    Executor, Limits, Provider, Questions,
    embedding::{Basis, distance},
    evidence::{EvaluatorIdentity, Policy},
    resolve,
};
use serde_json::{Value, json};
use std::{
    hint::black_box,
    io::{BufRead, BufReader, Read, Write},
    net::TcpListener,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{Duration, Instant},
};

fn main() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let endpoint = format!("http://{}", listener.local_addr().unwrap());
    let stop = Arc::new(AtomicBool::new(false));
    let done = stop.clone();
    let server = thread::spawn(move || {
        while !done.load(Ordering::Relaxed) {
            let Ok((mut socket, _)) = listener.accept() else {
                thread::sleep(Duration::from_millis(1));
                continue;
            };
            socket.set_nonblocking(false).unwrap();
            socket
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let mut reader = BufReader::new(socket.try_clone().unwrap());
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
            let answers: serde_json::Map<_,_> = request["questions"].as_object().unwrap().iter().map(|(id,q)| {
                let answer = match q["type"].as_str().unwrap() {
                    "choice" => json!({"type":"choice","choice":"a","probabilities":{"a":0.7,"b":0.3}}),
                    "score" => json!({"type":"score","score":0.7,"legend":{"0":"Low","1":"High"},"probabilities":{"0":0.3,"1":0.7}}),
                    _ => json!({"type":"noul","noul":0.7}),
                }; (id.clone(), answer)
            }).collect();
            let output = json!({"model":"fixture","answers":answers}).to_string();
            write!(
                socket,
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                output.len(),
                output
            )
            .unwrap();
        }
    });
    let provider = Provider {
        endpoint,
        model: "fixture".into(),
        revision: "1".into(),
        api_key: String::new(),
    };
    let questions: Questions = serde_json::from_value(json!({
        "need":{"type":"noul","instructions":"需要跟进吗？"},
        "kind":{"type":"choice","instructions":"Choose","criteria":{"a":"One","b":"Two"}},
        "rank":{"type":"score","instructions":"Rate","criteria":["Low","High"]}
    }))
    .unwrap();
    let rows: Vec<_> = (0..32)
        .map(|id| json!({"id":id,"text":"Task complete"}))
        .collect();
    let mut executor = Executor::new(
        provider.clone(),
        Limits {
            max_requests: 5000,
            max_judgments: 10000,
            max_input_bytes: 128_000_000,
            ..Limits::default()
        },
    )
    .unwrap();
    let output = executor.evaluate(&rows, &questions, || {}).unwrap();
    let start = Instant::now();
    for _ in 0..1000 {
        black_box(executor.evaluate(&rows, &questions, || {}).unwrap());
    }
    let warm_ms = start.elapsed().as_secs_f64() * 1000.0;
    let basis = Basis::parse(json!({"context_columns":["text"],"questions":questions})).unwrap();
    let evaluator: EvaluatorIdentity = (&provider).into();
    let start = Instant::now();
    for _ in 0..10000 {
        black_box(
            basis
                .project(&output[0].decisions, Some(evaluator.clone()))
                .unwrap(),
        );
    }
    let project_ms = start.elapsed().as_secs_f64() * 1000.0;
    let left = basis
        .project(&output[0].decisions, Some(evaluator.clone()))
        .unwrap();
    let changed = resolve(&json!({"model":"fixture","answers":{
        "need":{"type":"noul","noul":0.25},
        "kind":{"type":"choice","choice":"a","probabilities":{"a":0.6,"b":0.4}},
        "rank":{"type":"score","score":0.1,"legend":{"0":"Low","1":"High"},"probabilities":{"0":0.9,"1":0.1}}
    }}), &questions, "fixture", &Policy::default()).unwrap();
    let right = basis.project(&changed, Some(evaluator)).unwrap();
    let start = Instant::now();
    for _ in 0..20000 {
        black_box(distance(black_box(&left), black_box(&right)).unwrap());
    }
    let distance_ms = start.elapsed().as_secs_f64() * 1000.0;
    let start = Instant::now();
    let prepared_left = jev_executor::embedding::PreparedMatrix::new(&left).unwrap();
    let prepared_right = jev_executor::embedding::PreparedMatrix::new(&right).unwrap();
    for _ in 0..20000 {
        black_box(prepared_left.distance(black_box(&prepared_right)).unwrap());
    }
    let prepared_ms = start.elapsed().as_secs_f64() * 1000.0;
    let bulky: Questions =
        serde_json::from_value(json!({"q":{"type":"noul","instructions":"x".repeat(12000)}}))
            .unwrap();
    let before = executor.usage.requests;
    for cycle in 0..40 {
        let cold: Vec<_> = (0..16).map(|i| json!({"id":cycle*16+i})).collect();
        executor.evaluate(&cold, &bulky, || {}).unwrap();
        let hot: Vec<_> = (0..16).map(|i| json!({"hot":i})).collect();
        executor.evaluate(&hot, &bulky, || {}).unwrap();
    }
    println!(
        "{}",
        json!({"warm_32000_rows_ms":warm_ms,"project_10000_ms":project_ms,"distance_20000_ms":distance_ms,"prepared_distance_20000_ms":prepared_ms,"cache_churn_requests":executor.usage.requests-before})
    );
    stop.store(true, Ordering::Relaxed);
    server.join().unwrap();
}
