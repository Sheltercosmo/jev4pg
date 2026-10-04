# Reviewed application writes

The development preview executes ordinary `INSERT`, `UPDATE` and `DELETE` in PostgreSQL without copying the source table into Python. A small update can target a large table through its indexes. This interface is for reviewed application changes; PostgreSQL clients remain the appropriate interface for bulk migrations.

## Preview and commit

Send a reviewer-authenticated request to `POST /data/sql`:

```json
{
  "sql": "UPDATE orders SET status = 'closed' WHERE id = 42 AND status = 'resolved'",
  "max_affected": 1
}
```

Inspect `affected_rows`, `before_sample`, `changes_sample` and the SQL. The first 20 rows appear in primary-key order. Update values are evaluated and cast to their destination types in PostgreSQL, so the preview includes assignment rounding. An insert preview shows the supplied SQL expressions; defaults, triggers and constraints are applied at commit.

Commit with `POST /data/mutations/{preview_token}/commit`, using the same reviewer identity. A token lasts ten minutes and can commit once. Viewing a preview never holds row locks while the user decides. SQL submitted through natural-language planning uses this same write path after planning and review.

## What the review covers

For updates and deletes, the review covers the matching rows and their proposed values. At commit, PostgreSQL locks those rows and rechecks the preview within one repeatable-read transaction. A changed row, changed matching population, changed assignment result or changed table contract requires a new preview. Unrelated row changes do not invalidate it.

The final statement is constrained to the reviewed primary keys. A row that starts matching after the commit transaction's snapshot is excluded. Returned update and delete rows are compared with the reviewed result before committing. A difference rolls back the transaction, including a trigger that changes the direct result unexpectedly. These checks follow PostgreSQL's [row locking](https://www.postgresql.org/docs/17/explicit-locking.html#LOCKING-ROWS) and [repeatable-read semantics](https://www.postgresql.org/docs/17/transaction-iso.html#XACT-REPEATABLE-READ).

The preview does not simulate trigger or foreign-key side effects on other tables. Administrators remain responsible for those database rules. Constraints, privileges and row-level security remain enforced by PostgreSQL. An insert may therefore fail at commit even when its supplied expressions were previewed. No part of a failed batch is committed.

Concurrent changes can cause a serialization conflict, deadlock or lock timeout. The API returns HTTP 409 with a PostgreSQL error code and guidance to obtain a fresh preview. It does not silently retry a reviewed write against changed data.

## Limits

| Contract | Behavior |
| --- | --- |
| Source size | No source-row count or source-table copy for ordinary PostgreSQL writes. Indexes and query complexity still determine database work. |
| Reviewed batch | At most 1,000 affected rows; `max_affected` can set a smaller limit. Exceeding the limit fails instead of applying a partial batch. |
| Review payload | Before images and proposed values share a 4 MiB budget. A single value must be decoded before its size is checked. |
| Concurrency | Locks protect selected rows until commit. Unrelated writes remain possible. |
| SQL | One managed target table, named `INSERT VALUES`, or `UPDATE`/`DELETE` without mutation subqueries. Primary-key updates require a migration. |
| Full-table changes | Require `allow_all: true` and still obey the affected-row budget. |
| Timing | Ten-second statement timeout and three-second lock timeout. |
| Deletion | Associated evidence is removed and retained query results/history are redacted in the same transaction. PostgreSQL performs the history selection and redaction. |

The manifest reports `mutation_scope: "reviewed_targets"`. `source_rows` remains null with `source_rows_state: "NOT_EVALUATED"`; the service has not counted the whole table. `reviewed_rows` records the exact target count. Existing read-only attachments remain read-only.

The Python semantic mutation path and SQLite development path retain their earlier whole-source preview contract and 50,000-row limit. Semantic writes still require resolved membership. Expressions such as `CURRENT_TIMESTAMP` can change between preview and commit and require a new preview; supply an explicit timestamp when reviewing a fixed value.

## Validation

Run against a disposable PostgreSQL server:

```sh
python -m pytest tests/test_mutation_postgres.py -q -s
```

Set `SDD_TEST_ADMIN_URL` as described in the installation tests. The suite creates and removes its own database and roles. It checks a generated million-row table, concurrent writers, stale previews, composite keys, Simplified Chinese identifiers, NULLs, numeric rounding, row security, rollback and review limits. The timing record is written to `.runtime/mutation-validation.json`.

These are deterministic SQL and transaction tests. They do not measure natural-language interpretation accuracy or large-population semantic evaluation.

Local validation on PostgreSQL 17.11, 4 October 2026:

| Measurement | Result |
| --- | --- |
| Source population | 1,000,000 generated rows |
| One-row preview, median of eight requests | 10.57 ms |
| One-row commit, median of eight requests | 11.75 ms |
| Peak traced Python allocation for a 1,000-row preview and commit | 4.35 MiB |
| Provider calls | 0 |

Timing measures the SQL service on one machine with a warm cache and an indexed primary key, excluding HTTP and network transport. Memory excludes PostgreSQL, native driver allocations and total process memory. Four additional transaction cases passed after freezing the runtime, including a writer committing during lock acquisition and replacement of the physical table. The earlier 50,000-row source limit prevented this million-row workload; these figures are not a measured speedup over that path.
