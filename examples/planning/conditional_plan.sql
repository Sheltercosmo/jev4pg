-- Run with the configured native extension and permission to execute plans.
BEGIN ISOLATION LEVEL REPEATABLE READ;

SELECT jsonb_pretty(jev_native.execute_plan($plan$
{
  "version": 1,
  "target": "answer",
  "stages": [
    {
      "id": "messages",
      "operator": "source",
      "sql": "SELECT id,body,jsonb_build_object('output_state','VALUE','operation_state','SUCCEEDED','value',urgent,'raw','{}'::jsonb) AS route FROM (VALUES (1,'Please restore access today.',true),(2,'Thank you, the issue is resolved.',false)) AS m(id,body,urgent)",
      "columns": {
        "id": {"kind":"integer","label":"Message identity"},
        "body": {"kind":"text","label":"Message text"},
        "route": {"kind":"json","label":"Structured urgency decision"}
      },
      "keys": [["id"]]
    },
    {
      "id": "urgent",
      "operator": "semantic",
      "sql": "SELECT id,body,route FROM messages",
      "inputs": [{"stage":"messages","alias":"messages","require_values":[]}],
      "columns": {
        "id": {"kind":"integer","label":"Message identity"},
        "body": {"kind":"text","label":"Message text"},
        "route": {"kind":"json","label":"Routing decision"}
      },
      "row_guard": {"column":"route","equals":true},
      "questions": {
        "respond": {"type":"noul","instructions":"The message requests an immediate action that remains unresolved.","subject_column":"body"}
      }
    },
    {
      "id": "routine",
      "operator": "semantic",
      "sql": "SELECT id,body,route FROM messages",
      "inputs": [{"stage":"messages","alias":"messages","require_values":[]}],
      "columns": {
        "id": {"kind":"integer","label":"Message identity"},
        "body": {"kind":"text","label":"Message text"},
        "route": {"kind":"json","label":"Routing decision"}
      },
      "row_guard": {"column":"route","equals":false},
      "questions": {
        "respond": {"type":"noul","instructions":"The message contains an unresolved question or request that needs a follow-up response.","subject_column":"body"}
      }
    },
    {
      "id": "selected",
      "operator": "merge",
      "sql": "SELECT u.id,u.route,u.__jev_decisions->'respond' AS urgent_result,r.__jev_decisions->'respond' AS routine_result FROM urgent u JOIN routine r USING(id)",
      "inputs": [
        {"stage":"urgent","alias":"urgent","require_values":[]},
        {"stage":"routine","alias":"routine","require_values":[]}
      ],
      "columns": {
        "id": {"kind":"integer","label":"Message identity"},
        "route": {"kind":"json","label":"Routing decision"},
        "urgent_result": {"kind":"json","label":"Urgent review"},
        "routine_result": {"kind":"json","label":"Routine review"}
      },
      "keys": [["id"]],
      "selections": {
        "respond": {"selector":"route","cases":[
          {"equals":true,"column":"urgent_result"},
          {"equals":false,"column":"routine_result"}
        ]}
      }
    },
    {
      "id": "answer",
      "operator": "project",
      "sql": "SELECT id,__jev_decisions->'respond' AS decision FROM selected ORDER BY id",
      "inputs": [{"stage":"selected","alias":"selected","require_values":[]}],
      "columns": {
        "id": {"kind":"integer","label":"Message identity"},
        "decision": {"kind":"json","label":"Selected response decision"}
      }
    }
  ]
}
$plan$::jsonb, '{"max_requests":10,"concurrency":2}'::jsonb));

ROLLBACK;
