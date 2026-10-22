# Semantic filtering: jev4pg and pg-jev

Native preview 0.3.0 reduces repeated inference with exact context dictionaries, parallel request packing and evidence reuse across input windows. In this paired test, its mapped executor was faster on seven of eight workloads and classified real reviews more accurately. It did not win every latency or token comparison.

These are executor and HTTP request measurements, not an end-to-end PostgreSQL benchmark. The baseline uses unchanged request-building functions from [pg-jev 0.2.1](https://github.com/realZachi/pg-jev/tree/e3450d44f7f6745aa2d728c9289b9ca4a6feda9a), with concurrency, exact deduplication and warm caching supplied by the harness. PostgreSQL scans, indexes, LIMIT, read-ahead and durable registry overhead are outside the measurement.

## Results

Each cell lists **jev4pg / pg-jev request path**. Both receive complete records. Accuracy includes every judgment, including held answers, at the same probability threshold of 0.5. Latency is the median of three live runs with cold local caches.

| Workload | Accuracy | Latency | HTTP requests |
| --- | --- | --- | --- |
| One predicate | 100.00% / 98.75% | 1,170 / 1,010 ms | 13 / 20 |
| Three independent predicates | 100.00% / 99.33% | 1,498 / 1,983 ms | 38 / 60 |
| Conversational paraphrases | 100.00% / 99.28% | 1,715 / 2,102 ms | 38 / 60 |
| Simplified Chinese questions | 100.00% / 99.17% | 729 / 1,060 ms | 38 / 60 |
| Renamed Chinese columns | 100.00% / 99.08% | 989 / 1,507 ms | 38 / 60 |
| Repeated contexts | 100.00% / 100.00% | 287 / 302 ms | 1 / 3 |
| Wide records | 100.00% / 87.08% | 487 / 538 ms | 13 / 20 |
| Real customer reviews | 98.00% / 92.00% | 469 / 732 ms | 13 / 20 |

Each workload has 400 rows, except repeated contexts with 2,000 rows. Predicates concern occupations, EU membership and phrase presence; paraphrases and renamed schemas test representation changes, not unseen concepts. Reviews are 400 text-disjoint examples from the [UCI Sentiment Labelled Sentences dataset](https://archive.ics.uci.edu/dataset/331/sentiment+labelled+sentences), covering products, films and restaurants.

The operational policy uses 0.8/0.2 thresholds. For reviews, it held 12.6% of jev4pg judgments and 19.2% of baseline judgments as `UNKNOWN`; these remain in the accuracy denominator above. No HTTP failures or `NOT_EVALUATED` outcomes occurred. This is semantic filtering accuracy, not NL2SQL execution accuracy.

## Context reduction and cost

When required columns are declared, both methods can evaluate distinct values and map results back. We give the baseline the same projections; dictionary reduction is not claimed as exclusive to jev4pg.

| Input layout | jev4pg input tokens | Baseline input tokens | Difference |
| --- | ---: | ---: | ---: |
| Three predicates, complete records | 97,203 | 102,041 | 4.7% fewer |
| Three predicates, declared columns | 954 | 1,495 | 36.2% fewer |
| Reviews, complete records | 28,488 | 25,961 | 9.7% more |

With declared columns, the three predicates retain 100% accuracy on both sides. jev4pg packs the independent missing judgments into one request rather than three; median latency is 292 versus 339 ms. Projection must include every field the question needs. Wide contexts shared by many questions can favor the existing `scan` layout instead of `scan_mapped`.

Cost is reported as provider-metered input tokens; apply the price for your provider and model. Fewer requests do not necessarily mean fewer billed tokens. No LLM generation is used.

## Continuous input and caching

All warm passes reproduced their cold decisions with zero provider requests on both sides. For 2,000 repeated records and three predicates, median local replay was 6.42 ms versus 18.91 ms. For 400 unique reviews, it was 1.35 ms versus 1.20 ms; the baseline remained slightly faster.

The streaming test starts with 300 rows, repeats them, then sends 100 new rows and 100 known rows. For three predicates, arrivals required 10 requests versus 15. With declared columns, all arrival values were already known and both methods needed zero requests; local processing took 0.57 versus 1.59 ms. New rows alone do not require new inference when their required semantic context is unchanged. See [streaming measurements](semantic-filtering-stream-0.3.0.csv).

## Reproduce and interpret

Measured on 22 October 2026 using `jev-1.13.0`, concurrency 16 and a shared HTTP gateway. jev4pg packs up to 32 question-local records; the pinned upstream request builder uses 20 rows per condition. The native client is a Rust 1.96 release build on Windows. Linux/PostgreSQL integration is tested separately with a deterministic provider fixture.

Implementation [43218c4](https://github.com/Sheltercosmo/jev4pg/commit/43218c48c46c06a5e8454c1a278ba63d64543369) was frozen before `test_v2`. Development and validation used separate populations. Review splits are disjoint by normalized text hash; synthetic categories overlap. Method order alternates between repeats. Provider-side caching is not controlled, and three runs do not establish stable tail latency or statistical superiority.

[Run the benchmark](../../benchmarks/semantic_filtering/README.md) · [Aggregate measurements](semantic-filtering-0.3.0.csv) · [Source and measurement hashes](semantic-filtering-0.3.0.json) · [Use mapped SQL](../NATIVE_RECORD_MAP.md)

The result supports an advantage for these mapped semantic workloads. It does not establish a universal lead over pg-jev, a retrieval benchmark advantage, or production-scale PostgreSQL throughput. Use your own predicates, context widths and data distributions before selecting an execution layout.
