# BIRD Challenging: 100-question comparison

On 23 September 2026, the frozen Python planner was evaluated on 100 challenging questions across 11 BIRD databases, with one attempt per question for JEV, LLM and hybrid planning. These are historical measurements. They have not been rerun on v0.7.0 and do not measure the Rust executor.

## Results

| Method | SQL answer matches | Median time | p95 time | Estimated cost / 100 attempts |
| --- | ---: | ---: | ---: | ---: |
| JEV 1.13.0 | 20/99 (20.2%) | 8.55 s | 26.80 s | $0.389 |
| GPT-5.6 Terra | 39/99 (39.4%) | 8.68 s | 38.85 s | $3.262 |
| JEV + GPT-5.6 Terra | 34/99 (34.3%) | 14.23 s | 54.16 s | $2.839 |

JEV used zero LLM generation calls and 88.1% less estimated token cost than the LLM baseline. Hybrid used 20.8% fewer LLM input tokens, retaining 40.3% of catalog fields. It made one LLM call on 96 questions and two on four questions. Independent JEV requests overlapped in all 100 JEV cases and 99 hybrid cases, with a maximum concurrency of four. There was no serial comparison to isolate a speedup from parallelism.

These savings came with lower answer accuracy. Hybrid matched six answers the LLM missed and missed eleven the LLM matched; both matched 28. The run does not establish that hybrid is more accurate or faster.

## What was measured

Questions came from the challenging category of the [BIRD SQL Dev 20251106 release](https://huggingface.co/datasets/birdsql/bird_sql_dev_20251106), revision `3c11fb193e5439b338e23677fa0aae11e8b85db9`. A seeded selection balanced database coverage and excluded previously evaluated local question IDs. All 100 were held out from development during this run. Schemas could have been seen previously; model training exposure is unknown. This subset is not an official leaderboard submission or the full BIRD challenging split.

JEV used staged planning without LLM generation. The LLM baseline received the full catalog and bounded value observations. Hybrid used JEV context selection, compact SQL alternatives, parallel review and at most one repair. Both LLM paths used GPT-5.6 Terra with low reasoning effort through the local Codex CLI. All methods received the supplied BIRD evidence and shared SQLite data, SQL guards and execution scoring. Reference SQL and expected rows were withheld during planning.

One reference exceeded the 30-second limit in all three methods, leaving 99 common scorable questions. Held proposals were executed read-only for scoring. Both JEV methods held all 100 planning attempts, so their scores do not represent autonomous execution. Missing proposals and execution failures remained failures when the reference was available.

Scoring checks column position, duplicate counts, exact integers and other numbers within an absolute tolerance of 0.000001. Ordering is checked where the reference's sort keys can be mapped to its output; order remained unverified for ten questions. There were no empty-reference matches. Complete application results matched 20, 38 and 34 references respectively: one otherwise correct LLM query exceeded the application's 1,000-row return cap.

## Timing and cost

The table reports the 94 questions run with up to three cases in flight. Each case's methods ran sequentially in a balanced rotating order. Six isolated questions had median times of 8.67 s, 7.47 s and 8.70 s for JEV, LLM and hybrid. Each measurement includes planning, transport, CLI startup where applicable and application execution; reference execution and full-result diagnostics are excluded.

Costs cover all 100 attempts per method. They are API-equivalent estimates using frozen USD-per-million-token rates: LLM input 2, cached input 0.2, output 12, cache writes 2.5; JEV input 0.042. They are neither invoices nor current provider prices. One hybrid JEV request failed with HTTP 520 and supplied no usage, so its cost is omitted. No missing-usage calls were recorded in the other methods.

## Record and attribution

The [compact metrics record](bird-challenging-100.json) preserves aggregate results, model settings, source revision and the archived runtime fingerprint. The figures were checked against the 300 saved attempts when restoring this documentation. This is an archival summary, not a rerunnable benchmark package; source databases, prompts, reference SQL and raw traces are not included.

BIRD is maintained by the [BIRD benchmark authors](https://bird-bench.github.io/). The source dataset is licensed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/); that license applies to the benchmark material, separately from this project's Apache 2.0 source license.
