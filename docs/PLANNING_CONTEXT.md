# Planning context on large tables

The development preview collects bounded value evidence before interpreting a request. It reads several columns together and reuses that batch within the request. Planning no longer builds a full distinct-value dictionary or counts an entire column to infer a key.

Independent PostgreSQL table batches use up to four concurrent connections. Each batch receives a share of the remaining payload budget, so a large table cannot consume the whole allowance before smaller tables are inspected. This collection performs no JEV or LLM calls. Model decisions retain their existing parallel stages.

## What the planner can infer

Examples establish that a value was observed in the authorized source. They do not establish that other values are absent, identify the complete vocabulary, or change the rows used by the final SQL query. An incomplete dictionary alone does not hold a resolved query. Unresolved interpretation and failed checks still require review.

Registered primary keys remain structural evidence. A possible key inferred from data requires a complete, bounded population and a database-side uniqueness check. That check retains database comparison and collation rules. A partial sample cannot prove uniqueness. Inferred relationships remain proposals and never become registered constraints automatically.

Planning records separate `VALUE`, `UNKNOWN` and `NOT_EVALUATED` from collection status. A successful partial sample reports `TRUNCATED`; a field skipped by a budget reports `NOT_EVALUATED`; a failed collection reports `UNKNOWN` with a failure code. Sample metadata is excluded from the catalog-definition fingerprint, so changing examples does not masquerade as a schema change during review.

## Literal lookup

For PostgreSQL text fields, bounded literal hypotheses can find values outside the sample. Candidates come from quoted text, word sequences and Simplified Chinese character sequences in the request. Case variants are also considered. These are lookup hypotheses; JEV still decides whether a found value expresses a requested restriction.

Each candidate uses a parameterized [LATERAL lookup](https://www.postgresql.org/docs/17/queries-table-expressions.html#QUERIES-LATERAL) that returns at most one matching value. This avoids reading every occurrence of a common category. The service checks the [PostgreSQL execution plan](https://www.postgresql.org/docs/17/using-explain.html) and skips the lookup if the target cannot use an index condition. It does not fall back to a whole-column substring scan.

An absent lookup result is not proof that no interpretation exists. Literal extraction is bounded, and exact lookup does not cover arbitrary misspellings, translations or semantic equivalents. Unverified literals explicitly supplied by the user remain candidates when the observed dictionary is incomplete. Add useful indexes through normal database administration; the planner does not create them automatically.

## Limits and consistency

| Resource | Development default |
| --- | --- |
| Sample | Up to 512 rows and 32 columns per source batch; hybrid context retains its 256-row, 24-column limits. |
| Collection budget | 1 MiB of serialized row payload and 32 source operations per collector. Metadata and Python object overhead are additional. |
| Text and JSON | PostgreSQL clips values before transfer. Display examples use at most 160 characters. Clipped text is excluded from exact-value candidates. JSON examples are bounded text representations. |
| Literal hypotheses | At most 128 candidates per lookup. |
| Statement timeout | 750 ms for sample and bounded uniqueness statements; 500 ms for literal lookup. Source validation has its own limits. |
| Reuse | Within one request and tenant only. No persistent cache of source samples. |

Samples favor early primary-key rows when a key is available. They are not statistically representative. Complex views, row security and expensive source expressions may still require substantial PostgreSQL work; a row limit is not a general query-cost guarantee. Source pinning, permissions and [row-level security](https://www.postgresql.org/docs/17/ddl-rowsecurity.html) apply to both samples and probes. Independent sources have separate snapshots, and later query execution obtains its own snapshot.

## Reproduce validation

Set `SDD_TEST_ADMIN_URL` to a disposable PostgreSQL server, then run:

```sh
python -m pytest tests/test_planning_samples.py tests/test_planning_samples_postgres.py -q -s
python -m pytest tests/test_planning_samples_final_postgres.py -q
```

The PostgreSQL fixture creates and removes its own database and roles. It includes a generated million-row relation, indexed rare values outside the sample, English paraphrases and Chinese literals, hidden rows, unindexed lookups and large text/JSON values. `.runtime/planning-samples-validation.json` records the current catalog collection time without model calls.

On PostgreSQL 17.11, local validation on 4 October 2026 collected catalog context from a generated one-million-row, six-column table in a median 56.69 ms over six warm-cache runs. No JEV or LLM calls were made. This measures catalog preparation within the service, excluding HTTP transport, model inference and final query execution.

Additional cases first run against the frozen implementation verify that four source reads overlap, a growing population cannot inherit a small-sample uniqueness proof, and rare English and Chinese literals remain discoverable under renamed schemas. These cases now serve as regressions; future final evaluations need new untouched cases.

These tests establish collection and grounding contracts. They do not establish overall NL2SQL accuracy, translation quality or end-to-end semantic-query speed.
