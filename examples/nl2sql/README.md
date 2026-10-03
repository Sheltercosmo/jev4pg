# Natural language to SQL tutorial

Import the sample data, try the [example queries](../../docs/NL2SQL_EXAMPLES.md), or compare JEV, GPT-5.6 Terra and hybrid planning. See [performance and cost](../../docs/PERFORMANCE_AND_COST.md) for the recorded results and their scope.

## Use the data

[cases.json](cases.json) contains two six-row datasets, requests and expected answers. Each object in `datasets` can be submitted to `POST /datasets` with your access token. You can also enter its name, rows and column definitions through Manage data in the workspace.

Use `deliveries` and `exams` for the examples. `ledger_a` is a renamed copy for the schema check. Select the relevant dataset, paste a question from `cases` and preview the SQL before execution.

## Run the comparison

Install the project and configure a [JEV provider](../../docs/PROVIDERS.md). The recorded run used JEV 1.13.0 and a signed-in Codex CLI running `gpt-5.6-terra` with tools disabled.

Validate the data and references without model calls:

```bash
python examples/nl2sql/compare.py --validate-only
```

Run all three methods:

```bash
python examples/nl2sql/compare.py --model gpt-5.6-terra --output .runtime/tutorial-run.json
```

This consumes provider usage. To use the Responses API instead, configure `OPENAI_API_KEY` locally and add `--transport openai`. Choose a new output filename for each run.

The runner uses temporary SQLite databases and scores held SQL proposals inside the evaluation. Normal application approvals still apply. [results.json](results.json) contains generated SQL, result rows, measurements and the scoring protocol.
