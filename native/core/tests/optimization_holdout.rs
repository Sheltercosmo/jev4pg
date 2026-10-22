use jev_executor::{
    embedding::{Basis, distance, distances},
    evidence::{EvaluatorIdentity, Policy},
    resolve,
};
use serde_json::{Value, json};

#[test]
fn frozen_mixed_probability_bases_match_an_independent_distance_oracle() {
    let evaluator = EvaluatorIdentity {
        endpoint_sha256: "c".repeat(64),
        model: "holdout".into(),
        revision: "1".into(),
    };
    for case in 0..100 {
        let count = 2 + case % 9;
        let labels: Vec<_> = (0..count).map(|i| format!("级别 {i}")).collect();
        let basis = Basis::parse(json!({"context_columns":[format!("字段{case}")],"questions":{
            "是否":{"type":"noul","instructions":"是否需要处理？"},
            "category":{"type":"choice","instructions":"Select the applicable category.",
                        "criteria":{"甲":"第一类","乙":"第二类","未定":"需要更多信息"}},
            "rating":{"type":"score","instructions":"Rate on the declared scale.","criteria":labels}
        }})).unwrap();
        let mut raw_distributions = Vec::new();
        let matrices: Vec<_> = (0..3).map(|side| {
            let p = ((case * 17 + side * 31) % 101) as f64 / 100.0;
            let c = [(case + side + 1) as f64, 3.0, (side + 2) as f64];
            let c_sum: f64 = c.iter().sum();
            let c: Vec<_> = c.iter().map(|v| v / c_sum).collect();
            let levels: Vec<_> = (0..count).map(|i| ((i + 3 * side + case) % 11) as f64).collect();
            let sum: f64 = levels.iter().sum();
            let levels: Vec<_> = levels.iter().map(|v| v / sum).collect();
            raw_distributions.push(vec![c.clone(), levels.clone(), vec![1.0 - p, p]]);
            let probabilities: serde_json::Map<String, Value> = levels.iter().enumerate().map(|(i,p)| (i.to_string(),json!(p))).collect();
            let legend: serde_json::Map<String, Value> = labels.iter().enumerate().map(|(i,s)| (i.to_string(),json!(s))).collect();
            let response = json!({"model":"holdout","answers":{
                "是否":{"type":"noul","noul":p},
                "category":{"type":"choice","choice":"甲","probabilities":{"甲":c[0],"乙":c[1],"未定":c[2]}},
                "rating":{"type":"score","score":0.5,"legend":legend,"probabilities":probabilities}
            }});
            let decisions = resolve(&response,&basis.questions,"holdout",&Policy::default()).unwrap();
            basis.project(&decisions,Some(evaluator.clone())).unwrap()
        }).collect();
        for (a, query) in matrices.iter().enumerate() {
            let batch = distances(query, &matrices).unwrap();
            for (b, candidate) in matrices.iter().enumerate() {
                let squared: f64 = raw_distributions[a]
                    .iter()
                    .zip(&raw_distributions[b])
                    .map(|(x, y)| {
                        let coefficient: f64 = x.iter().zip(y).map(|(p, q)| (p * q).sqrt()).sum();
                        (1.0 - coefficient).clamp(0.0, 1.0)
                    })
                    .sum();
                let expected = (squared / 3.0).sqrt();
                assert!(
                    (batch[b] - expected).abs() < 3e-8,
                    "case {case}: {a} -> {b}"
                );
                assert!((distance(query, candidate).unwrap() - expected).abs() < 3e-8);
            }
        }
    }
}
