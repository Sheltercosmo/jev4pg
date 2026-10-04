# Operator examples

Install and start the database using the [repository setup](../../docs/INSTALLATION.md). The examples use `httpx`, which is included in the project dependencies. Run them from the repository root with the installed Python environment.

Set `SDD_TOKEN` to a database API token from your server's `SDD_API_TOKENS` configuration. Provider credentials belong to the server and are not sent by these clients. See [provider setup](../../docs/PROVIDERS.md).

PowerShell:

```powershell
$env:SDD_TOKEN = "your-database-api-token"
.\.venv\Scripts\python.exe examples/operators/workflow.py
.\.venv\Scripts\python.exe examples/operators/basic.py
```

Bash:

```bash
export SDD_TOKEN="your-database-api-token"
.venv/bin/python examples/operators/workflow.py
.venv/bin/python examples/operators/basic.py
```

| Example | What it demonstrates | Provider use |
|---|---|---|
| [workflow.py](workflow.py) | False gate followed by an unexecuted branch | None |
| [basic.py](basic.py) | Typed NOUL decision, explicit output state, bounded call | Up to one request |

The workflow prints:

```text
gate: VALUE / SUCCEEDED / False
stage2: NOT_EVALUATED / SKIPPED / None
```

The basic example's answer depends on the source and model. Try `--text "The work is promised for tomorrow."`; the client preserves `UNKNOWN` and failure states instead of treating them as false. Both scripts accept `--base-url` and `--help`.

For the other operators, copy a request from the [function reference](../../docs/JEV_FUNCTION_REFERENCE.md) or `GET /jev/operators/examples` into the same `client.post` pattern. Replace marked IDs with values from your own catalog or prior responses. Calls save audit records; governance examples describe the additional reviewer requirements.

## Text extraction and import

Run `python examples/operators/text_import.py` for a preview, or add `--commit` to create entries automatically when resolved. Set `SDD_TOKEN` to a reviewer token and optionally `SDD_URL`. The [text import guide](../../docs/TEXT_IMPORT.md) explains field descriptions, source evidence, missing-value policies and appending to existing datasets.

## Native SQL

Build and configure the [Rust extension](../../native/README.md) first. These scripts use synthetic data in your PostgreSQL session and require the native function grants. They call the configured JEV provider; results depend on its responses.

| Example | Purpose |
| --- | --- |
| [native_embedding.sql](native_embedding.sql) | Define a question basis, store answer probabilities and rank messages against one search input. |
| [native_plan.sql](../planning/native_plan.sql) | Compose SQL and semantic stages under a shared scheduler. |
| [conditional_plan.sql](../planning/conditional_plan.sql) | Route rows to selected questions and merge decisions without treating skipped work as false. |

From the repository root, connect using your configured PostgreSQL role:

```bash
psql -X -v ON_ERROR_STOP=1 -d your_database -f examples/operators/native_embedding.sql
```

See [embedding contracts](../../docs/NATIVE_EMBEDDINGS.md) and [stage dependencies](../../docs/NATIVE_PLANS.md) before adapting the examples to persistent tables.
