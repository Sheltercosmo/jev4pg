<p align="center">
  <img src="docs/assets/readme-banner.png" width="1000" alt="jevsd-pg — Natural language. Semantic SQL. Pangolin mascot with three parallel data paths." />
</p>

<p align="center">
  <strong>A self-developing SQL database with JEV based semantic operators and natural language queries</strong>
</p>

<p align="center">
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/version-0.6.0-18181b?style=flat-square&amp;labelColor=52525b" alt="Version 0.6.0" /></a>
  <a href="docs/INSTALLATION.md"><img src="https://img.shields.io/badge/Python-3.11%2B-18181b?style=flat-square&amp;labelColor=52525b" alt="Python 3.11 or newer" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache_2.0-18181b?style=flat-square&amp;labelColor=52525b" alt="Apache 2.0 license" /></a>
</p>

<p align="center">
  <a href="docs/INSTALLATION.md">Installation</a> ·
  <a href="docs/USER_GUIDE.md">User guide</a> ·
  <a href="docs/zh/USER_GUIDE.md">简体中文</a> ·
  <a href="docs/JEV_FUNCTION_REFERENCE.md">Operator reference</a>
</p>

Ask questions in plain language, inspect the SQL, and turn useful semantic decisions into reusable database features. jevsd-pg brings together 41 semantic operators, parallel JEV evaluation, cached evidence and optional LLM planning through a web workspace, HTTP API and PostgreSQL SQL interface.

The operators combine JEV's native Noul, Choice and Score primitives with database and workflow logic. The default JEV service is provided by TypeSafe. You can also connect a compatible third-party endpoint or a local model adapter. PostgreSQL handles storage, joins and arithmetic.

## Why jevsd-pg

* Query data in English or Simplified Chinese. Inspect the SQL, correct its interpretation and rerun it from query history.
* Choose JEV planning or hybrid planning. In hybrid mode, JEV selects relevant context, an LLM proposes SQL, and JEV reviews the proposal.
* Query text by meaning alongside structured data. Save reviewed definitions as reusable features, such as whether a message requests action.
* Extract database entries from documents. Describe the rows and columns, then review typed values alongside their source text before importing.
* Preview inserts, updates and deletes before committing them.
* Batch independent semantic decisions and reuse compatible evidence. Apply the same operators to extraction, ranking, matching, verification and conditional workflows.
* Use TypeSafe, a compatible hosted endpoint or a local Python model adapter without changing operator calls.

The self-developing part is the semantic layer: definitions, evidence and corrections can be saved, reviewed, reused and refreshed as data changes. New concepts require approval before promotion.

## From question to SQL

Given a `deliveries` table with supplier names and quantities:

> For each supplier, show the total quantity delivered, largest total first.

The JEV planner generated:

```sql
SELECT
  SUM("r0"."quantity") AS "result_1",
  "r0"."supplier" AS "result_2"
FROM "deliveries" AS "r0"
GROUP BY
  "r0"."supplier"
ORDER BY
  SUM("r0"."quantity") DESC NULLS LAST
```

Result: Birch: 36, Aster: 30, Cedar: 8. JEV selects the query structure; SQL performs the calculation.

See [verified examples](docs/NL2SQL_EXAMPLES.md) for filtering, averages and the runnable dataset. Preview a proposal, inspect its decisions, and approve it when it matches your intent.

## Getting started

With Python 3.11 or newer and Docker Compose v2:

```bash
git clone https://github.com/Sheltercosmo/jevsd-pg.git
cd jevsd-pg
python deploy/configure.py
docker compose build
docker compose up -d --wait
```

Configuration creates local credentials and asks for your TypeSafe key. You can instead configure a [compatible endpoint or local model](docs/PROVIDERS.md). Hybrid mode also needs an LLM provider.

Open the [English workspace](http://127.0.0.1:8000/ask/en) or [Simplified Chinese workspace](http://127.0.0.1:8000/ask/zh). Run `python deploy/configure.py --show-token` to retrieve your workspace token, then import data through Manage data.

The stack includes PostgreSQL 17, schema migration, the API and a durable SQL worker. See [installation](docs/INSTALLATION.md) for an existing PostgreSQL server, upgrades and backups. [SQL client examples](docs/POSTGRESQL_INTERFACE.md) show how to submit JEV operators directly from `psql`.

## Query your data

Select a dataset, enter your question and choose Preview plan to inspect the SQL. You can edit a planning decision, select an alternative interpretation or edit the SQL directly. Run the query when the proposal matches your intent. Data changes require a separate commit.

The same workflow is available through `POST /ask`:

```json
{
  "question": "For each supplier, show the total quantity delivered, largest total first.",
  "dataset_ids": ["deliveries"],
  "planner_mode": "hybrid",
  "execute": false
}
```

Use an ID or name from your catalog in `dataset_ids`. Set `planner_mode` to `jev` to plan without LLM generation. See the [query API guide](docs/NATURAL_LANGUAGE.md) and the local [API reference](http://127.0.0.1:8000/docs) for authentication and complete requests.

## Semantic operators

The API provides 41 operators and `WORKFLOW`. For example, send this request to `POST /jev/call` to test a proposition:

```json
{
  "operator": "JEV.NOUL",
  "arguments": {
    "state": "The shipment arrived on Tuesday.",
    "proposition": "The shipment has arrived."
  },
  "limits": {"max_judgments": 1, "max_requests": 1}
}
```

Results distinguish a known value, an unknown answer and work that was not evaluated. Independent judgments can run in parallel; compatible evidence can be reused.

Use the [operator guide](docs/JEV_OPERATORS.md) to select an operator and set budgets. The [function reference](docs/JEV_FUNCTION_REFERENCE.md) includes arguments, examples and result contracts for every operator.

## Simple-query performance

JEV matched GPT-5.6 Terra's results on the version 0.5.0 tutorial without LLM generation.

| Method | Matching SQL proposals | Median time |
| --- | ---: | ---: |
| JEV | 11/11 | 5.51 s |
| GPT-5.6 Terra | 11/11 | 4.94 s |
| JEV + GPT-5.6 Terra | 11/11 | 7.21 s |

The sample contains eight basic objectives and three paired variations on small synthetic datasets. All JEV and hybrid proposals required review and were scored through execution. Terra timing includes CLI startup. Complex queries require a separate evaluation.

See [performance and cost](docs/PERFORMANCE_AND_COST.md) for measurement details and the [runnable tutorial](examples/nl2sql/README.md) to reproduce the check.

## Documentation and contributions

See the [documentation index](docs/README.md) for text imports, semantic features and architecture. To report a problem, include a small synthetic dataset, the request and the expected result. Development setup and checks are in [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache 2.0](LICENSE). Third-party software retains its own licenses. See [NOTICE](NOTICE) and [dependencies](docs/DEPENDENCIES.md).
