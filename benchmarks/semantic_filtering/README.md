# Semantic filtering benchmark

Compare the native Rust executor and mapped execution in preview 0.3.0 with the request builder from [pg-jev](https://github.com/realZachi/pg-jev), pinned to commit `e3450d44f7f6745aa2d728c9289b9ca4a6feda9a` (0.2.1). See the [published comparison](../../docs/benchmarks/SEMANTIC_FILTERING.md).

This measures executor and HTTP request paths. It does not compare full PostgreSQL scans, indexes, read-ahead, LIMIT behavior, durable registries or end-to-end SQL throughput. The extracted upstream functions remain unchanged; the harness supplies scheduling and exact deduplication. No upstream benchmark score is presented as our measurement.

## Run

Build the Rust example, prepare the public reviews, and use a private environment variable for authentication:

```sh
git clone https://github.com/realZachi/pg-jev .runtime/pg-jev-reference
git -C .runtime/pg-jev-reference checkout e3450d44f7f6745aa2d728c9289b9ca4a6feda9a
cargo build --release --locked --manifest-path native/Cargo.toml -p jev-executor --example measure
python benchmarks/semantic_filtering/prepare_reviews.py --output .runtime/semantic-filtering/reviews.json
python benchmarks/semantic_filtering/run.py --split development_v2 --live \
  --reviews .runtime/semantic-filtering/reviews.json \
  --workloads single,three_predicates,paraphrased,chinese,renamed,repeated_contexts,wide,reviews \
  --methods native_map,pgjev_request_path,native_dictionary,pgjev_dictionary \
  --output reports/semantic-filtering/development.json
python benchmarks/semantic_filtering/summarize.py reports/semantic-filtering/development.json \
  --output reports/semantic-filtering/development.csv
```

Set `TYPESAFE_API_KEY` or `SDD_JEV_API_KEY` locally. `--env-file` reads a private configuration file; `--proxy` configures the common forwarding connection. `--native` selects a target-specific binary, including `.exe` on Windows. `httpx` is the only third-party Python dependency. Omitting `--live` uses a transport fixture and reports no language-accuracy score.

## Methods and controls

| Method | Request layout | Context |
| --- | --- | --- |
| `native` | One row and its independent questions | Complete fixture row |
| `native_map` | Up to 32 question-local records | Complete fixture row |
| `pgjev_request_path` | Upstream shared array, 20 rows per condition | Complete fixture row |
| `native_dictionary` | Question-local records after exact projection | Declared dependent columns |
| `pgjev_dictionary` | Upstream request path after the same projection | Identical declared columns |

The same model (`jev-1.13.0`), inputs, predicates, gateway and concurrency limit of 16 apply to every method. Both sides receive exact duplicate elimination and warm reuse. Dictionary results must be compared to the dictionary baseline; their context reduction is not exclusive to this project. Projection declarations belong to fixtures, not production benchmark-recognition rules.

Runs alternate method order, repeat three times by default, and start with fresh local caches. Cold, warm and optional arrival passes have separate latency and metered request/token counts. `--stream` starts with 75% of the split, repeats that population, then sends the remaining 25% plus a known 25%. The provider can have its own undisclosed caching; "cold" means local cache cold.

## Splits and scoring

The current splits are `development_v2`, `validation_v2` and `test_v2`, with 400 rows per ordinary workload. Duplicate workloads contain five times as many rows. Earlier split aliases remain available for reproduction; the current comparison uses the v2 holdout. Controlled roles, countries and phrase predicates follow the categories in pg-jev's published filtering example. Paraphrases, Simplified Chinese, renamed Chinese fields and wider records check representation changes. Their categories overlap between splits; they are not unseen concepts or independent generalization tasks.

Real reviews come from the [UCI Sentiment Labelled Sentences dataset](https://archive.ics.uci.edu/dataset/331/sentiment+labelled+sentences), Kotzias (2015), DOI `10.24432/C57604`, CC BY 4.0. The preparation script rejects conflicting duplicate labels and creates deterministic, text-disjoint splits by content hash. The archive hash is recorded. Data remains local; the repository contains no downloaded corpus.

Use development and validation to change the implementation. Freeze source, harness and binary hashes before test. Keep final failures as evidence; using them to change behavior requires a new untouched test. The report records source/binary hashes, model, platform, request outcomes and every decision. Existing report paths are never overwritten.

All judgments count in accuracy at a common probability threshold of 0.5, including held answers. A separate 0.8/0.2 policy reports resolved accuracy, UNKNOWN and NOT_EVALUATED. Warm passes must reproduce decisions without provider requests. Three repeats show variability; they are insufficient for a stable tail-latency estimate. Raw reports stay under ignored `reports/` and are not release documentation.
