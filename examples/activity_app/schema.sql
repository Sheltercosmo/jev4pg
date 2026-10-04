-- Run in a development database as its source owner.
CREATE SCHEMA activity_demo;
CREATE TABLE activity_demo.events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    category text NOT NULL,
    amount numeric(18,2),
    note text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX events_category_id ON activity_demo.events (category, id);

INSERT INTO activity_demo.events(category, amount, note, created_at)
SELECT CASE WHEN mod(n,7)=0 THEN 'warning' ELSE 'normal' END,
       n::numeric / 100, 'Generated example event ' || n,
       now() - (10000-n) * interval '1 second'
FROM generate_series(1,10000) n;
