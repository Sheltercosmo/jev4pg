BEGIN ISOLATION LEVEL REPEATABLE READ;

WITH answer AS MATERIALIZED (
  SELECT jev_native.execute_plan(
    '{
      "version": 1,
      "target": "total",
      "stages": [
        {
          "id": "messages",
          "operator": "semantic",
          "sql": "SELECT note FROM (VALUES (''The repair is complete.''),(''Still waiting for parts.'')) records(note)",
          "columns": {"note": {"kind": "text", "label": "Work description"}},
          "questions": {"done": {"type": "noul", "instructions": "Has the work been completed?"}}
        },
        {
          "id": "total",
          "operator": "aggregate",
          "inputs": [{"stage": "messages", "alias": "messages", "require_values": ["done"]}],
          "sql": "SELECT count(*) AS completed FROM messages WHERE jev_native.require_bool(__jev_decisions, ''done'')",
          "columns": {"completed": {"kind": "integer", "label": "Completed work", "nullable": false}},
          "grain": [],
          "keys": [[]]
        }
      ]
    }'::jsonb,
    '{"max_rows":100,"max_requests":10,"concurrency":4}'::jsonb
  ) AS result
)
SELECT result->'rows' AS rows,
       result->'stages' AS stages,
       result->'usage' AS usage
FROM answer;

COMMIT;
