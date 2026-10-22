# Native and operator execution performance

The 0.4 native preview and shared HTTP runtime reduce evidence reads, request preparation and repeated probability computation. Measurements compare the v0.8.0 implementation with frozen source [c0c2491](https://github.com/Sheltercosmo/jev4pg/commit/c0c2491dce8f5ca0d5a2eb90d0a985595ec690ba).

| Workload | v0.8.0 | Optimized | Result |
| --- | ---: | ---: | --- |
| Read 600 cached operator judgments | 162.1 ms | 8.0 ms | 20.4× faster; 600 SELECTs become 3 |
| Pack 1,024 questions over a shared context | 22.5 ms | 7.4 ms | 3.0× faster; identical 32 request bundles |
| Project 10,000 probability matrices | 61.1 ms | 16.7 ms | 3.7× faster |
| Run 20,000 scalar embedding comparisons | 14.3 ms | 12.2 ms | 15% less time |
| Compare the same prepared pair 20,000 times | 14.3 ms, scalar | 0.91 ms, prepared | 15.7× faster with both matrices prepared once |
| Cache pressure: 640 new contexts plus 16 recurring contexts | 688 requests | 656 requests | 32 redundant requests removed |
| Return 32,000 cached native rows | 170.9 ms | 185.9 ms | 9% higher median; variable across runs |

The prepared comparison is a Rust reuse API. SQL `embedding_distances` prepares the query once per candidate batch; its full PostgreSQL latency is not the prepared-pair timing. Warm scan timing includes evidence copying and LRU maintenance. This run does not establish a warm-scan latency improvement.

## Method

Measured on 22 October 2026, Windows x86-64, Intel Core Ultra 9 285H. Rust 1.96 release builds use the same local deterministic HTTP fixture. Native timings are medians of nine alternating paired runs. Operator timings are medians of seven alternating paired runs using Python 3.13 and SQLite in memory. The native baseline is source [28a0cd1](https://github.com/Sheltercosmo/jev4pg/commit/28a0cd1e13af97a804cac700b7d46d12dcb238dc), shipped as v0.8.0 with native 0.3.0.

The fixture exercises NOUL, Choice and Score, including Chinese text. Cache pressure alternates 16 new contexts with 16 recurring contexts for 40 cycles. Each question has 12,000 instruction characters. The optimized request count equals the 656 unique contexts. All model costs here are zero because the provider is local; request counts describe potential inference savings, not a billing measurement.

These tests measure execution and computation. Natural-language accuracy and the separate semantic-filtering comparison retain their own evaluation protocols.

## Reproduce

From the repository root:

```sh
python -m scripts.benchmark_operator_runtime --baseline-ref v0.8.0
cd native
cargo run --release --locked -p jev-executor --example measure_local
cargo test --locked -p jev-executor
```

The operator benchmark loads runtime and store code from the trusted local baseline revision. The native baseline used the same `measure_local` workload, omitting only the new `PreparedMatrix` timing block and output field. Runs alternate baseline and current executables to reduce ordering effects.

## Correctness

The native suite passes 48 tests. Its final holdout adds 100 mixed probability bases and 900 pair comparisons against an independently calculated Hellinger distance, with Chinese labels, renamed context fields, varying rubric widths and endpoint probabilities. The affected HTTP operator suite passes 56 tests, including latest valid evidence, timestamp ties, invalidation, tenant isolation, exact UTF-8 packing, duplicate outputs and policy replay.

The implementation was frozen before the holdout. These are execution-correctness checks; they do not assign a language-understanding score. See [function coverage, placement and SQL usage](../NATIVE_OPTIMIZATION.md).

[PostgreSQL 17 verification](https://github.com/Sheltercosmo/jev4pg/actions/runs/39850367205) passes 134 checks, including batch distances, parallel branches, row security, cancellation and upgrades from native 0.1–0.3 to 0.4. Eight independent 100 ms fixture requests complete in 211 ms through the shared scheduler versus 828 ms sequentially. [Native container verification](https://github.com/Sheltercosmo/jev4pg/actions/runs/39849066138) also passes restart, backup restore and zero-call evidence reuse.
