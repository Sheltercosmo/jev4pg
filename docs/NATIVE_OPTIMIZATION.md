# Native execution optimization

Native preview 0.4.0 reduces repeated computation across the SQL functions. Independent work keeps the shared stage DAG and concurrency limit. The source SELECT, questions, provider requests and complete probability distributions retain their meaning.

The [performance record](benchmarks/NATIVE_OPTIMIZATION.md) reports paired timings, request counts and correctness checks against v0.8.0.

## What each function reuses

| Function | Optimization |
| --- | --- |
| `scan`, `scan_many`, `execute_plan` | Prepare active questions once, reuse exact observations before registry lookup, serialize each outgoing request once, and validate each fresh response once. Fully cached frontiers skip the async scheduler. |
| `scan_mapped` | Share projected-context hashing among questions using the same fields. Compute packed-question sizes once. Missing judgments still share bounded parallel requests. |
| `embed` | Use the same optimized executor and project probabilities directly from validated typed answers. |
| `answer_matrix` | Borrow raw distributions instead of building and decoding an intermediate response. |
| `decide` | Validate the observation envelope and question set once, then apply the current policy locally. |
| `require_bool` | Read the selected typed Boolean without copying its raw evidence. Unresolved or failed values still raise. |
| `embedding_distance` | Validate vectors without allocating a second flattened vector. |
| `embedding_distances` | Prepare one query once for a batch of candidates; return exact distances in candidate order. |

The executor retains an 8 MB serialized cache allowance. Eviction removes the least recently used entries until the new entry fits. Hot evidence survives unrelated new inputs. Threshold changes reclassify saved probabilities under the new policy; failed and unexecuted work never enters the cache. Changing a registry scope clears invocation-local reuse.

## Shared HTTP operator runtime

The `JEV.*` HTTP operators use their own shared runtime. It now reads evidence for up to 256 distinct keys per SQL query, using the newest non-invalidated observation for each key. Tenant, source revisions, questions and evaluator identity still constrain reuse. A second bulk lookup under the existing request lock catches work completed by another caller. Fully cached stages create no worker pool.

Batch construction serializes each question once and adds its exact UTF-8 size to a shared context envelope. This removes repeated serialization of progressively larger question bundles. The same 32-question, context and budget limits apply to NOUL, Choice and Score work throughout filtering, ranking, extraction, matching, verification and composed workflows. Local arithmetic and control operators continue to use zero model calls.

## Fewer judgments and requests

Apply exact SQL filters first. Declare every field needed by each question in `scan_mapped`. If two questions need separate columns with U and V distinct contexts, their dictionaries need U + V judgments, rather than evaluating both against every combination of columns. Mapping restores every source row, including duplicates.

Use `noul`, `choice` and `score` in the same independent frontier. Questions sharing projected context reuse its hash; missing judgments pack into requests of at most 32 questions. Candidate labels and score rubrics remain part of evidence identity. A similarity score cannot authorize reuse of another record's judgment.

For embeddings, mapped scans can feed `answer_matrix` as a local SQL projection. Project the basis's complete context for every question and pass the evaluator from the returned observation. This combines request packing with the same interpretable probability dimensions; inspect missing decisions before using a complete embedding.

## Exact local comparison

The distance is the root mean of squared Hellinger distances, with each question weighted equally. A prepared matrix stores `sqrt(p / sum(p))` for each answer. Repeated comparisons use subtraction and accumulation over those prepared values; they need no new JEV request or repeated square roots for that matrix.

Use `jev_native.embedding_distances(query, candidates)` for up to 512 candidate matrices. It accepts a JSON array and returns one-based ordinals and distances. The query is limited to 2 MB and the candidate array to 8 MB. Every matrix must be complete and use the same basis and evaluator revision. An incompatible candidate rejects the batch.

```sql
WITH candidates AS MATERIALIZED (
    SELECT document_id, embedding,
           row_number() OVER (ORDER BY document_id) AS ordinal
    FROM stored_embeddings
    ORDER BY document_id
    LIMIT 256
), packed AS (
    SELECT jsonb_agg(embedding ORDER BY ordinal) AS embeddings
    FROM candidates
)
SELECT c.document_id, d.distance
FROM packed p
CROSS JOIN LATERAL jev_native.embedding_distances(
    :query_embedding, COALESCE(p.embeddings, '[]'::jsonb)
) d
JOIN candidates c ON c.ordinal = d.ordinal
ORDER BY d.distance, c.document_id;
```

Choose the candidate population with exact SQL before comparison. For a larger population, iterate bounded batches and combine their rankings. Rust callers can retain `embedding::PreparedMatrix` values to reuse preparation on both sides of repeated comparisons.

## Installation and validation

Build the current development checkout for native 0.4.0. The released application tag v0.8.0 contains native 0.3.0. Install the matching native library and SQL files, restart PostgreSQL to load the library, then run `ALTER EXTENSION jev_native UPDATE TO '0.4.0'` or the matching application migration. The upgrade preserves stored evidence and existing scan grants.

`measure_local` exercises cache reuse, cache pressure, probability projection and exact distance with a local deterministic provider. Run `cargo run --release --locked -p jev-executor --example measure_local` from `native/`. These are computation and request-count measurements; the provider fixture does not measure language accuracy.
