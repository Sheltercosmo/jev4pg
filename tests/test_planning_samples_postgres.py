"""Planning source work on a generated million-row population; no model calls."""

import json
import os
from pathlib import Path
from statistics import median
from time import perf_counter

import pytest
from sqlalchemy import event, text

from test_deployment_postgres import installation as installation
from sdd.generic.catalog import Catalog
from sdd.generic.planning_samples import PlanningSamples


pytestmark = pytest.mark.skipif(
    not os.getenv("SDD_TEST_ADMIN_URL"), reason="Dedicated PostgreSQL server required"
)


@pytest.fixture(scope="module")
def corpus(installation):
    catalog = Catalog(installation["app"])
    dataset = catalog.create(
        "tenant-a",
        "records",
        [],
        primary_key=["id"],
        columns=[
            {"name": "id", "type": "integer"},
            {"name": "place", "type": "text"},
            {"name": "位置", "type": "text"},
            {"name": "code", "type": "text"},
            {"name": "state", "type": "text"},
            {"name": "note", "type": "text"},
        ],
    )
    relation = f'sdd_data."{dataset["table_name"]}"'
    with installation["owner"].engine.begin() as conn:
        conn.exec_driver_sql(f"""INSERT INTO {relation}
            SELECT n, CASE WHEN n=999999 THEN 'North Harbor' ELSE 'South' END,
                   CASE WHEN n=999998 THEN '星河站' ELSE '本地' END,
                   'ref-' || lpad(n::text,7,'0'),
                   CASE WHEN mod(n,2)=0 THEN 'open' ELSE 'closed' END,
                   CASE WHEN n=999997 THEN 'private note' ELSE 'ordinary' END
            FROM generate_series(1,1000000) n""")
        conn.exec_driver_sql(f"CREATE INDEX ON {relation} (place)")
        conn.exec_driver_sql(f'CREATE INDEX ON {relation} ("位置")')
        conn.exec_driver_sql(f"ANALYZE {relation}")
    return {**installation, "catalog": catalog, "dataset": dataset, "relation": relation}


def test_one_source_sample_reused_across_fields(corpus):
    samples = PlanningSamples(corpus["catalog"])
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if (
            corpus["dataset"]["table_name"] in statement
            and statement.lstrip().startswith("SELECT")
            and "LIMIT" in statement
            and "pg_catalog" not in statement
        ):
            statements.append(statement)

    event.listen(corpus["app"].engine, "before_cursor_execute", record)
    try:
        for name in ("code", "place", "state", "位置"):
            sample = samples.column("tenant-a", corpus["dataset"], name)
            assert len(sample.rows) == 512
            assert sample.operation_state == "TRUNCATED" and not sample.complete
        assert samples.reads == 1
        assert len(statements) == 1
        assert "DISTINCT" not in statements[0] and "LIMIT" in statements[0]
    finally:
        event.remove(corpus["app"].engine, "before_cursor_execute", record)


@pytest.mark.parametrize(
    "question,column,value",
    [
        ("Show North Harbor records", "place", "North Harbor"),
        ("Please list records for north harbor", "place", "North Harbor"),
        ("列出星河站的记录", "位置", "星河站"),
    ],
)
def test_indexed_literal_outside_sample(corpus, question, column, value):
    samples = PlanningSamples(corpus["catalog"])
    sample = samples.column("tenant-a", corpus["dataset"], column)
    assert value not in sample.evidence(column)["examples"]
    found = samples.matches("tenant-a", corpus["dataset"], column, question)
    assert found["operation_state"] == "SUCCEEDED", found
    assert value in found["values"]
    before = samples.reads
    assert samples.matches("tenant-a", corpus["dataset"], column, question) is found
    assert samples.reads == before


def test_unindexed_search_skips_execution_and_unique_is_unknown(corpus):
    samples = PlanningSamples(corpus["catalog"])
    found = samples.matches("tenant-a", corpus["dataset"], "note", "Find 'private note'")
    assert found["output_state"] == "NOT_EVALUATED" and found["values"] == []
    assert samples.unique("tenant-a", corpus["dataset"], "code") is None
    injected = samples.matches("tenant-a", corpus["dataset"], "place", "' OR 1=1 --")
    assert injected["values"] == []


def test_hidden_rows_are_not_observed_by_samples_or_probes(corpus):
    catalog = corpus["catalog"]
    dataset = catalog.create(
        "tenant-a",
        "private_places",
        [{"id": n, "place": "Secret" if n == 600 else "Visible"} for n in range(1, 601)],
        primary_key=["id"],
    )
    relation = f'sdd_data."{dataset["table_name"]}"'
    with corpus["owner"].engine.begin() as conn:
        conn.exec_driver_sql(f"CREATE INDEX ON {relation} (place)")
        conn.exec_driver_sql(f"CREATE POLICY hidden ON {relation} AS RESTRICTIVE USING (id<>600)")
    samples = PlanningSamples(catalog)
    assert samples.matches("tenant-a", dataset, "place", "Secret")["values"] == []
    sample = samples.sample("tenant-a", dataset)
    assert "Secret" not in sample.evidence("place")["examples"]
    assert samples.sample("tenant-b", dataset).rows == []


def test_wide_payload_is_cut_in_database(corpus):
    dataset = corpus["catalog"].create(
        "tenant-a", "documents", [{"id": 1, "body": "small", "document": {}}], primary_key=["id"]
    )
    with corpus["owner"].engine.begin() as conn:
        conn.execute(
            text(f'''UPDATE sdd_data."{dataset["table_name"]}"
            SET body=repeat('字',1000000),document=json_build_object('long',repeat('x',1000000))''')
        )
    sample = PlanningSamples(corpus["catalog"]).sample("tenant-a", dataset)
    assert len(sample.rows[0]["body"]) == 161 and len(sample.rows[0]["document"]) == 161
    assert sample.evidence("body")["exact_values"] == []
    assert sample.evidence("document")["text_may_be_truncated"]


def test_model_catalog_million_row_measurement(corpus):
    catalog, dataset = corpus["catalog"], corpus["dataset"]
    times = []
    for _ in range(6):
        start = perf_counter()
        result = catalog.model_catalog(
            "tenant-a",
            [dataset["id"]],
            question="Show North Harbor records",
            include_features=False,
        )
        times.append((perf_counter() - start) * 1000)
        columns = {c["name"]: c for c in result[0]["columns"]}
        assert "North Harbor" in columns["place"]["values"]
        assert columns["place"]["value_evidence"]["complete"] is False
    report = {
        "population": 1000000,
        "columns": 6,
        "source": "generated",
        "repetitions": 6,
        "catalog_median_ms": round(median(times), 2),
        "provider_calls": 0,
        "limits": "SQL service only; local warm cache; samples are not a complete vocabulary.",
    }
    Path(".runtime/planning-samples-validation.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report))


def test_hybrid_retained_fields_receive_indexed_rare_values(corpus):
    from sdd.generic.hybrid_feedback import observed_context

    dataset = corpus["dataset"]
    packet = {"request": "列出North Harbor和星河站的记录", "catalog": [dataset]}
    output, trace = observed_context("tenant-a", packet, corpus["catalog"])
    columns = {c["name"]: c for c in output["catalog"][0]["columns"]}
    assert "North Harbor" in columns["place"]["value_evidence"]["examples"]
    assert "星河站" in columns["位置"]["value_evidence"]["examples"]
    assert trace["tables"] == 1 and trace["operation_state"] == "TRUNCATED"
    assert columns["place"]["value_evidence"]["complete"] is False
