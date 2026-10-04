CREATE TEMP TABLE example_basis AS
SELECT '{
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
}'::jsonb AS definition;

CREATE TEMP TABLE example_messages(id integer, body text);
INSERT INTO example_messages VALUES
    (1, 'Please check why my invoice is incorrect.'),
    (2, 'The invoice has been corrected. Thank you.'),
    (3, '发票金额不正确，请帮忙核查。');

CREATE TEMP TABLE example_embeddings AS
SELECT result.*
FROM example_basis,
LATERAL jev_native.embed(
    'SELECT id,body FROM example_messages ORDER BY id',
    definition,
    '{"max_requests":3,"max_judgments":6,"concurrency":3}'
) AS result;

SELECT source->>'id' AS id,
       embedding->'axes' AS axes,
       embedding->'probabilities' AS answer_probabilities,
       embedding->'vector' AS vector,
       embedding->'question_states' AS question_states
FROM example_embeddings
ORDER BY ordinal;

WITH query_embedding AS MATERIALIZED (
    SELECT result.embedding
    FROM example_basis,
    LATERAL jev_native.embed(
        $$SELECT 'An invoice needs investigation.' AS body$$,
        definition
    ) AS result
)
SELECT document.source->>'id' AS id,
       jev_native.embedding_distance(document.embedding, query.embedding) AS distance
FROM example_embeddings AS document
CROSS JOIN query_embedding AS query
WHERE (document.embedding->>'complete')::boolean
  AND (query.embedding->>'complete')::boolean
ORDER BY distance, id;
