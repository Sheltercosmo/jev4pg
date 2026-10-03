# Performance and cost

On the version 0.5.0 tutorial, JEV matched GPT-5.6 Terra's SQL results without LLM generation. Terra was slightly faster; hybrid added semantic review.

## Simple queries

| Method | Matching SQL proposals | Median time |
| --- | ---: | ---: |
| JEV 1.13.0 | 11/11 | 5.51 s |
| GPT-5.6 Terra | 11/11 | 4.94 s |
| JEV + GPT-5.6 Terra | 11/11 | 7.21 s |

Measured on 3 October 2026 using eight basic objectives and three paired checks for paraphrasing, Simplified Chinese and schema renaming. The two small synthetic datasets cover aggregation, filtering and ordering. This tutorial does not establish parity on complex SQL.

All JEV and hybrid proposals required review. Their SQL was executed inside the evaluation to score the answers. Timing includes planning and SQLite execution; Terra also includes CLI startup. Both Terra modes used low reasoning effort. These are single-run measurements.

The [tutorial](../examples/nl2sql/README.md) provides the data and runner. [Detailed results](../examples/nl2sql/results.json) contain every generated query, scoring rules, usage and source hashes.

## Cost

Actual charges were not measured. Using the [Terra API rates](https://developers.openai.com/api/docs/models/gpt-5.6-terra) checked on 3 October 2026, recorded LLM usage corresponds to about $0.10 for the baseline and $0.13 for hybrid across 11 queries, before JEV charges. These estimates do not represent Codex subscription billing.

JEV charges depend on your provider. Set `SDD_JEV_INPUT_USD_PER_MILLION` to its input-token rate. The default `0.042` is an accounting assumption; include any output or local compute costs separately. The recorded token counts remain available in the result file for recalculation.

## Tuning

* Reuse compatible evidence to avoid repeating model judgments. Provider, revision, source or question changes can require fresh evaluation.
* Adjust `SDD_JEV_CONCURRENCY` and `SDD_PLANNING_WORKERS` to control parallel requests and planning work. Both default to 4 and allow up to 16.
* Set operator budgets for requests, judgments, tokens and estimated cost. Use `JEV.EXPLAIN_PLAN` to estimate missing work before dispatch.

[Hybrid configuration](HYBRID_QUERY.md) controls optional concept and repair calls. [Provider setup](PROVIDERS.md) covers local batching and concurrency. [Operator usage](JEV_OPERATORS.md) explains budgets and result completeness.
