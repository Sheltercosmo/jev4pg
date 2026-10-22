-- Run after installing and configuring jev_native. This invokes the provider.
BEGIN ISOLATION LEVEL REPEATABLE READ;

CREATE TEMP TABLE work_items (id bigint PRIMARY KEY, status text);
INSERT INTO work_items VALUES
    (1, 'Waiting for a response'),
    (2, 'Waiting for a response'),
    (3, 'Completed'),
    (4, NULL);

-- The question depends only on status. Keep IDs out of the inference context.
CREATE TEMP TABLE status_decisions AS
SELECT source->>'status' AS status, decisions
FROM jev_native.scan(
    'SELECT DISTINCT status FROM work_items ORDER BY status NULLS LAST',
    '{"follow_up":{"type":"noul","instructions":"Does the status indicate that someone must follow up?","subject_column":"status"}}',
    '{"max_rows":1000,"max_judgments":1000,"max_requests":1000}'
);

-- Map back before counting, so duplicate source records keep their multiplicity.
SELECT w.id, w.status, d.decisions->'follow_up' AS decision
FROM work_items AS w
JOIN status_decisions AS d ON w.status IS NOT DISTINCT FROM d.status
ORDER BY w.id;

-- Exact membership requires resolved evidence; NULL subjects must be handled explicitly.
SELECT count(*) AS items_needing_follow_up
FROM work_items AS w
JOIN status_decisions AS d ON w.status = d.status
WHERE CASE WHEN d.status IS NULL THEN false
           ELSE jev_native.require_bool(d.decisions, 'follow_up') END;

ROLLBACK;
