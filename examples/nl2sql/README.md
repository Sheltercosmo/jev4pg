# Natural language to SQL tutorial

This example compares the version 0.5.0 JEV planner, GPT-5.6 Terra and their hybrid on eight basic query objectives and three paired variations. All three methods produced 11 matching SQL proposals in the recorded run. All JEV and hybrid proposals required review.

## Files

| File | Contents |
| --- | --- |
| [cases.json](cases.json) | Two six-row synthetic tables, a renamed copy, requests, reference SQL and expected rows. |
| [compare.py](compare.py) | Live comparison runner using the published planner and guarded SQL executor. |
| [results.json](results.json) | Generated SQL, result rows, timing, usage, model names and source hashes. |

## Use the data in the workspace

Open `cases.json`. Each object in `datasets` is a dataset import body containing `name`, `description`, `columns`, `primary_key` and `rows`. Submit it to `POST /datasets` with your access token, or enter its name, rows and column definitions through Manage data in the workspace. The ordinary examples use `deliveries` and `exams`; `ledger_a` is the renamed delivery table for the paired check.

Select the relevant dataset and paste a question from `cases`. Preview its SQL before execution. See [verified examples](../../docs/NL2SQL_EXAMPLES.md) for captured queries and results.

## Run the comparison

Install the project and configure a JEV provider as described in [installation](../../docs/INSTALLATION.md). The recorded run used TypeSafe JEV 1.13.0 and an installed, signed-in Codex CLI with `gpt-5.6-terra` at low reasoning effort. The CLI has tools disabled and receives neither references nor execution feedback.

Validate the data and references without model calls:

```bash
python examples/nl2sql/compare.py --validate-only
```

Run all three methods using a fresh output filename:

```bash
python examples/nl2sql/compare.py --model gpt-5.6-terra --output .runtime/tutorial-run.json
```

This sends data to the configured providers and consumes model usage. For an API account with access to that model, set `OPENAI_API_KEY` locally and add `--transport openai`. API timing will differ from the recorded CLI timing.

Every method and case receives a fresh temporary SQLite database. The runner preserves held proposals and executes them only inside the evaluation. It never bypasses the application's approval flow. Existing reports cannot be overwritten.

## Scope

The objectives cover counts, distinct counts, totals, averages, grouping, filtering and ordering. The three variants test a paraphrase, Simplified Chinese and renamed schema identifiers. They are paired checks, not three additional independent objectives. Source data includes NULLs, repeated group values and tied scores; no reference result is empty.

The dataset and expected rows were fixed before generation. The production code stayed unchanged. This tutorial was not used to tune the planner and is not a held-out benchmark. Generalization to complex SQL, larger catalogs or unfamiliar domains needs a separate evaluation. [Performance and cost](../../docs/PERFORMANCE_AND_COST.md) describes the scoring and timing boundaries.
