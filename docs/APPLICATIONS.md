# Large tables and application reads

Available from v0.7.0: table browsing, cursor pagination, reviewed CSV import and background SQL queries.

Keep large datasets in PostgreSQL. Attach an existing table instead of importing a copy through the browser. PostgreSQL retains its indexes, types, grants and row-level security; jev4pg supplies the catalog, query interface and semantic execution layer. Attachments are currently read-only and local to the service's PostgreSQL database. See [source onboarding](SOURCE_MANIFESTS.md).

## Load and query large data

Use your usual PostgreSQL loader, `COPY` or an ingestion tool to load a user-owned schema. For a CSV with a known schema, run `\copy app.events FROM '/absolute/path/events.csv' WITH (FORMAT csv, HEADER true)` in `psql`. Define keys and useful indexes, run `ANALYZE`, grant the runtime role access, then attach the relation:

```sh
sdd attach events --tenant demo --schema app --table events
```

The browser CSV importer is for small, reviewed imports: 5 MB and 10,000 rows. It checks all rows, preserves identifiers with leading zeros, and allows type corrections before creating a table. Use PostgreSQL bulk loading for larger files.

For API imports, send `name` and UTF-8 `content` to `POST /datasets/csv/preview`. Optional settings are `delimiter`, `null_empty` and explicit `columns`. The response includes inferred columns, a sample, conversion errors and a fingerprint. After reviewing a valid preview, send the same content and settings with its `columns` and `fingerprint` to `POST /datasets/csv`. A changed file or type definition requires another preview. Both routes require a reviewer token.

Ordinary SQL filters, joins, windows and aggregates execute in PostgreSQL. The HTTP SQL endpoint returns at most 1,000 rows and 4 MiB of compact JSON row data. It retains an exact prefix and reports `manifest.result_limited_by` as `rows`, `bytes` or null, with `result_bytes` and both configured limits. A first row that cannot fit fails explicitly; values are never shortened or replaced with NULL to fit a response.

PostgreSQL measures the cumulative UTF-8 value size before transferring the result. A server cursor fetches up to 128 rows at a time, with the batch size adjusted from the first row. Rows past the database byte boundary carry only an internal size marker; the application stops before exposing them. A second check accounts for JSON escaping and exact-number serialization. Table browsing uses the same transfer guard and retains its cursor budget.

These are data-size limits, not a 4 MiB process-memory guarantee. Driver objects, JSON decoding and database execution have separate costs. SQLite checks serialized size after decoding. Native stage DAGs retain their existing 8 MB native result limit before applying the application row budget. Use the table scan below for interactive pages, or a PostgreSQL client for a full export in one consistent snapshot. A small result limit does not make an unindexed filter or expensive aggregation cheap.

## Cursor API

Call `POST /datasets/{dataset_id}/scan` with a reader or reviewer bearer token:

```json
{
  "columns": ["id", "category", "amount"],
  "filters": [
    {"column": "category", "op": "eq", "value": "warning"},
    {"column": "amount", "op": "gte", "value": "25.50"}
  ],
  "limit": 100,
  "after": null
}
```

The response includes `result`, `columns`, `primary_key`, `returned_rows`, `has_more`, `next_after` and `execution_ms`. Send `next_after` as the next request's `after`, retaining the same filters and projection. Stop when `has_more` is false. `after` is a typed position, not an authorization token; every request independently checks dataset access.

Rows use ascending primary-key order, including composite keys. The reader uses a keyset predicate and does not issue a population count or a growing `OFFSET`. Index equality filters followed by the primary key when this matches the workload, for example `(category, id)`. Use PostgreSQL's `EXPLAIN (ANALYZE, BUFFERS)` on a representative query to verify the plan. Do not create an index for every possible filter.

| Contract | Behavior |
| --- | --- |
| Page limit | 1–1,000 rows, default 100. Row payloads plus cursor values have a conservative 4 MiB budget. |
| Read memory | PostgreSQL checks cumulative value bytes before driver decoding; bounded server-cursor batches avoid a source-table copy in Python. |
| Filters | Up to 16 conditions combined with AND: `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`, `prefix`, `is_null`, `not_null`. `in` accepts 1–100 non-null values. |
| Values | Typed and bound as parameters. Text prefixes are literal, including `%` and `_`. JSON columns support null checks here; use SQL for other JSON predicates. |
| Precision | Decimals and integers outside JavaScript's safe range are JSON strings. Keep them as strings or use an exact numeric library. |
| Keys | An exposed primary key is required. Views without one remain available through bounded SQL. |
| Consistency | `live_keyset`: one transaction per page. Changing keys or filter membership can skip or repeat records across requests. |
| Limits | Ten-second statement timeout and three-second lock timeout. `page_limited_by` distinguishes row and byte boundaries. Oversized individual rows fail with guidance to reduce the projection. |
| History | Interactive SQL and natural-language queries retain query history. Application scan requests do not create a history record for every page. |

Keep credentials on the application backend. Use a reader identity, expose only needed fields and filters, and map authenticated users to permitted identities on the server. The [activity monitor example](../examples/activity_app/README.md) shows this request flow with a reusable HTTP pool and error handling.

## Semantic work over large populations

Relational filtering and semantic evaluation have different costs. Filter and project in PostgreSQL before JEV calls where doing so preserves the requested population. Use reviewed semantic features or compatible evidence reuse for repeated work. Independent JEV stages remain parallel; dependent stages wait for their inputs.

The Python semantic path has a shared 50,000-source-row limit, including semantic mutation previews. It stops reading additional tables as soon as that budget is exceeded. Ordinary PostgreSQL writes use [bounded target previews and row locks](APPLICATION_WRITES.md), without copying the source table. The opt-in native executor supports bounded semantic scans and stage DAGs; consult [native deployment](NATIVE_DEPLOYMENT.md) and [plan coverage](NATIVE_PLANS.md). A fast table-page benchmark does not demonstrate million-row inference performance.

## Reproduce application validation

Set `SDD_TEST_ADMIN_URL` to a dedicated disposable PostgreSQL 17 server and run:

```sh
python -m pytest tests/test_application_scale_postgres.py -q -s
```

The test creates and removes its own database and roles. It generates one million events, exposes 800,000 through row-level security, checks filtered deep pages and database aggregates, and exercises 40 API requests with eight concurrent clients. Timings and Python allocation measurements are written to `.runtime/application-scale-validation.json`. This is a generated application workload, not an NL2SQL accuracy benchmark or a production capacity claim.

Local validation on PostgreSQL 17.11, 4 October 2026:

| Measurement | Result |
| --- | --- |
| Sequential 100-row API page, median of 8 requests | 18.73 ms |
| Eight concurrent clients, 40 requests, median / p95 | 90.57 / 133.02 ms |
| Concurrent request throughput | 82.9 requests/s |
| Peak traced Python allocation across five page reads | 0.28 MiB |
| JEV / LLM calls | 0 / 0 |

These are warm-cache, same-machine API measurements through FastAPI's test client. The allocation figure excludes PostgreSQL, native driver allocations and total process memory. Timing varied across local runs; use the reproduction command on your hardware with your indexes and data distribution.
