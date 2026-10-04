# Native JEV embeddings

The development Rust extension represents a record through its answer probabilities to a fixed set of questions. Questions share one context and one request. Independent records use the existing concurrent scheduler, request limits and evidence registry.

Each record returns a question-by-answer probability matrix and a flattened vector. A collection of records therefore forms a record-by-dimension matrix suitable for storage, inspection and similarity queries. The dimensions have named meanings defined by the questions and their answers.

This interface requires the [native extension](../native/README.md). It runs directly in PostgreSQL and does not require the Python service.

## Define a basis and embed records

Use the same basis for documents and search inputs. Declare the context fields explicitly; source identifiers can remain in the result without entering the model request.

```sql
SELECT source->>'id' AS id,
       embedding->'axes' AS axes,
       embedding->'probabilities' AS probabilities,
       embedding->'vector' AS vector,
       embedding->'question_states' AS states
FROM jev_native.embed(
    'SELECT id,body FROM messages ORDER BY id',
    '{
      "context_columns": ["body"],
      "questions": {
        "action": {
          "type": "noul",
          "instructions": "The message requests further action.",
          "subject_column": "body"
        },
        "resolved": {
          "type": "noul",
          "instructions": "The message reports that the issue is resolved.",
          "subject_column": "body"
        }
      }
    }',
    '{"max_requests":100,"max_judgments":200,"concurrency":4}'
);
```

For probabilities 0.8 and 0.3, the matrix would be `[[0.2,0.8],[0.7,0.3]]` and the vector `[0.2,0.8,0.7,0.3]`. These numbers illustrate the layout; actual results come from the configured provider.

| Question type | Answer dimensions |
| --- | --- |
| `noul` | `false`, `true`, with probabilities `1-p`, `p`. |
| `choice` | Every declared category, ordered by category ID. |
| `score` | Every rubric level, in numeric level order. |

Question IDs are ordered lexically. `axes` records each question ID, type and ordered answer IDs. Matrix rows can have different lengths; the vector concatenates them in that recorded order. No answer category is dropped.

A basis accepts 1 to 32 independent questions and up to 256 named context fields. Every subject column must be included. Its serialized definition is limited to 1 MB. Question and answer identities are limited to 200 bytes. Use a preceding SQL projection to normalize field names when embedding records from different schemas.

## Results and missing work

`embed` returns `ordinal`, `source`, `embedding`, `decisions`, `observation`, `usage`, `receipt` and `policy`. The source retains its identifiers and duplicates. Only `context_columns` enter inference and evidence identity. Identical contexts with different source IDs can reuse the same observation.

The embedding includes its basis fingerprint, evaluator identity, axes, probabilities, vector and per-question states. Its `complete` field means every probability distribution is available.

| Situation | Matrix cells | State |
| --- | --- | --- |
| Valid response with a resolved decision | Probabilities | Probability and decision states are VALUE. |
| Valid response with an uncertain decision | Probabilities | Probability state is VALUE; decision state remains UNKNOWN. |
| NULL subject or unselected branch | JSON null | NOT_EVALUATED, with SKIPPED retained. |
| Exhausted budget or provider failure | JSON null | NOT_EVALUATED, with the original operational status retained. |
| Empty source | No result rows | No model request. |

An incomplete embedding has `output_state: NOT_EVALUATED` and `operation_state: BLOCKED_BY_DEPENDENCY`. Inspect `question_states` for the cause of each missing distribution. A zero probability is a measured value; it never represents missing work. Probabilities are provider outputs, not a claim of calibrated certainty.

Decision thresholds do not change the probability coordinates. With the registry configured, compatible observations can supply embeddings under zero new-call allowance. Saved observations retain their model, revision and context identity through the existing evidence contract.

## Store and search

The [runnable SQL example](../examples/operators/native_embedding.sql) creates a basis, embeds three messages, stores the results and ranks them against one search input.

Stored embeddings represent the projected content at evaluation time. Regenerate them when that content or the basis changes.

`jev_native.embedding_distance(left, right)` computes root mean squared Hellinger distance across questions. Each question has equal weight regardless of its number of answers. Distributions are normalized locally for distance; stored probabilities remain unchanged. Zero means identical distributions and one means disjoint distributions for every question.

Both embeddings must be complete and have identical basis fingerprints, axes and evaluator identities. Changed question wording, category definitions, context fields or model revisions require a compatible new set of embeddings. Reordering JSON keys does not change the basis.

```sql
SELECT document_id,
       jev_native.embedding_distance(embedding, :query_embedding) AS distance
FROM stored_embeddings
WHERE (embedding->>'complete')::boolean
ORDER BY distance, document_id
LIMIT 10;
```

Embed the search input once using the stored basis before running this query. Distance comparisons perform no model calls. This query performs exact comparisons over the selected stored rows. Retrieval quality depends on the question basis and provider; the deterministic integration tests establish execution behavior, not retrieval accuracy.

## Reuse existing decisions and stage graphs

`jev_native.answer_matrix(basis, decisions, evaluator DEFAULT NULL)` projects existing typed decisions without provider I/O. It reads the questions named by the basis, so a subset can reuse a larger shared batch. Missing questions retain null cells. Pass the evaluator identity from their observation when the result will be compared with another embedding:

```sql
SELECT jev_native.answer_matrix(
    :basis,
    decisions,
    observation->'evaluator'
)
FROM saved_semantic_results;
```

This works as a local SQL stage after a native semantic stage. That stage must use the basis's questions and `context_columns`. Set the projection's predecessor `require_values` to `[]` so valid UNKNOWN decisions can still contribute probabilities. Check the resulting embedding's `complete` field before consuming all dimensions. Independent graph branches retain the shared scheduler; matrix projection adds no JEV stage.

Evaluator metadata passed to `answer_matrix` is a caller assertion. Stored matrices and observations use ordinary PostgreSQL ownership and access controls. A matrix without evaluator metadata remains inspectable, but distance rejects it.

## Installation and cost

Administrators grant `EXECUTE ON FUNCTION jev_native.embed(text,jsonb,jsonb)` and schema usage to the SQL role. The application migration's `--native-interface` option includes this grant. The role still needs SELECT access to its source columns, and row security applies.

The [native execution limits](../native/README.md#states-and-limits) apply. Questions sharing a context count as separate judgments within one request. Repeated contexts can reuse evidence, and local projection and distance add no provider calls. `usage` is cumulative across the invocation; take its maximum rather than summing it across result rows. Keep exact filters inside `source_sql` to reduce the population before dispatch.

Matrices add storage proportional to the number of answer dimensions. Keep the basis compact for broad scans and save the basis definition alongside stored embeddings.
