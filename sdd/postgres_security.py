"""Apply tenant RLS and immutable evidence guards as the schema owner."""

from contextlib import nullcontext
from sqlalchemy import text
from sdd.schema import metadata
from sdd.generic import schema as generic_schema  # noqa: F401 - register generic tables
from sdd.operators import schema as operator_schema  # noqa: F401 - register operator tables


def secure(db, connection=None):
    if db.engine.dialect.name != "postgresql":
        raise ValueError("PostgreSQL required")
    with nullcontext(connection) if connection is not None else db.engine.begin() as cx:
        for table in metadata.sorted_tables:
            name = table.name
            cx.execute(text(f'ALTER TABLE "{name}" ENABLE ROW LEVEL SECURITY'))
            cx.execute(text(f'ALTER TABLE "{name}" FORCE ROW LEVEL SECURITY'))
            cx.execute(text(f'DROP POLICY IF EXISTS tenant_isolation ON "{name}"'))
            cx.execute(
                text(f'''CREATE POLICY tenant_isolation ON "{name}"
                USING (tenant = current_setting('sdd.tenant', true))
                WITH CHECK (tenant = current_setting('sdd.tenant', true))''')
            )
        cx.execute(
            text("""CREATE OR REPLACE FUNCTION sdd_reject_evidence_update() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Evidence revisions are immutable'; END $$""")
        )
        for name in (
            "source_versions",
            "evaluator_revisions",
            "decision_policy_revisions",
            "observations",
            "human_assertions",
            "dataset_row_versions",
            "dataset_semantic_answers",
            "dataset_feature_assertions",
            "jev_operator_observations",
            "jev_operator_assertions",
            "jev_operator_definitions",
        ):
            cx.execute(text(f'DROP TRIGGER IF EXISTS immutable_revision ON "{name}"'))
            cx.execute(
                text(f'''CREATE TRIGGER immutable_revision BEFORE UPDATE ON "{name}"
                FOR EACH ROW EXECUTE FUNCTION sdd_reject_evidence_update()''')
            )
        cx.execute(
            text("""CREATE OR REPLACE FUNCTION sdd_protect_concept_definition() RETURNS trigger
          LANGUAGE plpgsql AS $$ BEGIN
            IF (to_jsonb(NEW) - 'status' - 'review') IS DISTINCT FROM (to_jsonb(OLD) - 'status' - 'review')
            THEN RAISE EXCEPTION 'Create a new concept revision instead of mutating its definition'; END IF;
            RETURN NEW;
          END $$""")
        )
        cx.execute(text("DROP TRIGGER IF EXISTS immutable_concept ON concept_revisions"))
        cx.execute(
            text("""CREATE TRIGGER immutable_concept BEFORE UPDATE ON concept_revisions
            FOR EACH ROW EXECUTE FUNCTION sdd_protect_concept_definition()""")
        )

        cx.execute(
            text("""CREATE OR REPLACE FUNCTION sdd_protect_feature_definition() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
              IF (to_jsonb(NEW) - 'status' - 'review' - 'materialization') IS DISTINCT FROM
                 (to_jsonb(OLD) - 'status' - 'review' - 'materialization') THEN
                RAISE EXCEPTION 'Create a new semantic feature revision';
              END IF;
              RETURN NEW;
            END $$""")
        )
        cx.execute(text("DROP TRIGGER IF EXISTS immutable_feature ON dataset_feature_revisions"))
        cx.execute(
            text("""CREATE TRIGGER immutable_feature BEFORE UPDATE ON dataset_feature_revisions
            FOR EACH ROW EXECUTE FUNCTION sdd_protect_feature_definition()""")
        )
