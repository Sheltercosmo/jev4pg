use crate::{Evaluation, evidence::Policy, resolve};
use std::collections::{BTreeMap, HashMap};

struct Entry {
    result: Evaluation,
    policy: Policy,
    result_bytes: usize,
    bytes: usize,
    access: u64,
}

pub(crate) struct EvaluationCache {
    entries: HashMap<String, Entry>,
    order: BTreeMap<u64, String>,
    clock: u64,
    bytes: usize,
    capacity: usize,
}

impl EvaluationCache {
    pub fn new(capacity: usize) -> Self {
        Self {
            entries: HashMap::new(),
            order: BTreeMap::new(),
            clock: 0,
            bytes: 0,
            capacity,
        }
    }

    pub fn clear(&mut self) {
        self.entries.clear();
        self.order.clear();
        self.bytes = 0;
        self.clock = 0;
    }

    pub fn get(
        &mut self,
        key: &str,
        policy: &Policy,
    ) -> Result<Option<(Evaluation, usize)>, &'static str> {
        let Some(entry) = self.entries.get_mut(key) else {
            return Ok(None);
        };
        if &entry.policy != policy {
            let observation = entry
                .result
                .observation
                .as_ref()
                .expect("Only observations enter the cache");
            let current = resolve(
                &observation.response,
                &observation.questions,
                &observation.evaluator.model,
                policy,
            )?;
            // Keep caller-local skipped decisions alongside the reinterpreted observations.
            entry.result.decisions.extend(current);
            let size = serde_json::to_vec(&entry.result)
                .map_err(|_| "Invalid cached result")?
                .len();
            let old_size = entry.result_bytes
                + serde_json::to_vec(&entry.policy)
                    .expect("Policy serializes")
                    .len();
            let new_size = size + serde_json::to_vec(policy).expect("Policy serializes").len();
            self.bytes = self.bytes - old_size + new_size;
            entry.bytes = entry.bytes - old_size + new_size;
            entry.result_bytes = size;
            entry.policy = policy.clone();
        }
        self.order.remove(&entry.access);
        self.clock += 1;
        entry.access = self.clock;
        self.order.insert(entry.access, key.to_owned());
        let result = (entry.result.clone(), entry.result_bytes);
        self.evict();
        Ok(Some(result))
    }

    pub fn insert(
        &mut self,
        key: String,
        result: &Evaluation,
        policy: &Policy,
        result_bytes: usize,
    ) {
        if result.observation.is_none() {
            return;
        }
        let bytes = 2 * key.len()
            + result_bytes
            + serde_json::to_vec(policy).expect("Policy serializes").len()
            + 128;
        if bytes > self.capacity {
            return;
        }
        if let Some(old) = self.entries.remove(&key) {
            self.order.remove(&old.access);
            self.bytes -= old.bytes;
        }
        self.clock += 1;
        self.order.insert(self.clock, key.clone());
        self.entries.insert(
            key,
            Entry {
                result: result.clone(),
                policy: policy.clone(),
                result_bytes,
                bytes,
                access: self.clock,
            },
        );
        self.bytes += bytes;
        self.evict();
    }

    fn evict(&mut self) {
        while self.bytes > self.capacity {
            let (_, key) = self.order.pop_first().expect("Accounted entry exists");
            let entry = self
                .entries
                .remove(&key)
                .expect("Cache order matches its entries");
            self.bytes -= entry.bytes;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Provider, Questions, evidence::Observation};
    use serde_json::json;

    fn sample() -> Evaluation {
        let questions: Questions =
            serde_json::from_value(json!({"q":{"type":"noul","instructions":"完成了吗？"}}))
                .unwrap();
        let provider = Provider {
            endpoint: "http://fixture".into(),
            model: "test".into(),
            revision: "1".into(),
            api_key: String::new(),
        };
        let (observation, decisions) = Observation::capture_with_policy(
            &json!({"note":"Done"}),
            &questions,
            &provider,
            json!({"model":"test","answers":{"q":{"type":"noul","noul":0.7}}}),
            &Policy::default(),
        )
        .unwrap();
        Evaluation {
            decisions,
            observation: Some(observation),
            receipt: None,
        }
    }

    #[test]
    fn evicts_only_the_least_recent_entry_and_preserves_hot_evidence() {
        let result = sample();
        let policy = Policy::default();
        let size = serde_json::to_vec(&result).unwrap().len();
        let entry_size = 2 + size + serde_json::to_vec(&policy).unwrap().len() + 128;
        let mut cache = EvaluationCache::new(2 * entry_size);
        for key in ["a", "b"] {
            cache.insert(key.into(), &result, &policy, size);
        }
        assert!(cache.get("a", &policy).unwrap().is_some());
        cache.insert("c".into(), &result, &policy, size);
        assert!(cache.get("b", &policy).unwrap().is_none());
        assert!(cache.get("a", &policy).unwrap().is_some());
        assert!(cache.get("c", &policy).unwrap().is_some());
        assert_eq!(cache.bytes, 2 * entry_size);
        cache.insert("a".into(), &result, &policy, size);
        assert_eq!(cache.bytes, 2 * entry_size);
        let policy = Policy {
            accept: 0.65,
            ..policy
        };
        let (replayed, actual_size) = cache.get("a", &policy).unwrap().unwrap();
        assert_eq!(replayed.decisions["q"].require_bool(), Ok(true));
        assert_eq!(actual_size, serde_json::to_vec(&replayed).unwrap().len());
        assert!(cache.bytes <= cache.capacity);
        assert_eq!(replayed.observation, result.observation);
    }

    #[test]
    fn blocked_results_and_oversized_entries_do_not_displace_evidence() {
        let result = sample();
        let policy = Policy::default();
        let mut cache = EvaluationCache::new(0);
        cache.insert("x".into(), &result, &policy, 100);
        assert!(cache.entries.is_empty());
        let mut cache = EvaluationCache::new(4096);
        let mut failed = result;
        failed.observation = None;
        cache.insert("x".into(), &failed, &policy, 100);
        assert!(cache.entries.is_empty());
    }
}
