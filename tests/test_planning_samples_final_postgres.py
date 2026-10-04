"""Fresh source-collection cases first run after the planning runtime freeze."""

import os
from threading import Barrier, Lock

import pytest
from sqlalchemy import event

from test_deployment_postgres import installation as installation
from sdd.generic.catalog import Catalog
from sdd.generic.planning_samples import PlanningSamples


pytestmark = pytest.mark.skipif(
    not os.getenv("SDD_TEST_ADMIN_URL"), reason="Dedicated PostgreSQL server required"
)


def test_independent_table_reads_overlap_in_one_collector(installation):
    catalog = Catalog(installation["app"])
    datasets = [
        catalog.create(
            "tenant-a", f"parallel_{n}", [{"id": 1, "value": f"group {n}"}], primary_key=["id"]
        )
        for n in range(4)
    ]
    relations = {d["table_name"] for d in datasets}
    arrivals, lock, rendezvous = set(), Lock(), Barrier(4, timeout=10)

    def meet(conn, cursor, statement, parameters, context, executemany):
        if not statement.lstrip().startswith("SELECT") or "LIMIT" not in statement:
            return
        if "pg_catalog" in statement:
            return
        relation = next((name for name in relations if name in statement), None)
        if relation is not None:
            with lock:
                arrivals.add(relation)
            rendezvous.wait()

    event.listen(installation["app"].engine, "before_cursor_execute", meet)
    try:
        collector = PlanningSamples(catalog)
        results = collector.many("tenant-a", [(d, ["value"]) for d in datasets])
    finally:
        event.remove(installation["app"].engine, "before_cursor_execute", meet)
    assert arrivals == relations
    assert collector.reads == 4
    assert [r.rows for r in results] == [[{"value": f"group {n}"}] for n in range(4)]
    assert all(r.complete and r.operation_state == "SUCCEEDED" for r in results)


def test_population_growth_cannot_turn_small_sample_into_key_proof(installation):
    catalog = Catalog(installation["app"])
    dataset = catalog.create(
        "tenant-a",
        "growing_source",
        [{"id": 1, "code": "a"}, {"id": 2, "code": "b"}],
        primary_key=["id"],
    )
    collector = PlanningSamples(catalog)
    assert collector.column("tenant-a", dataset, "code").complete
    with installation["owner"].engine.begin() as conn:
        conn.exec_driver_sql(
            f'INSERT INTO sdd_data."{dataset["table_name"]}" '
            "SELECT n, 'new-' || n::text FROM generate_series(3,700) n"
        )
    assert collector.unique("tenant-a", dataset, "code") is None


@pytest.mark.parametrize(
    "question,expected",
    [
        ("只看属于“云岚中心”的记录", "云岚中心"),
        ("Which entries belong to 'Amber Quay'?", "Amber Quay"),
    ],
)
def test_rare_literals_survive_schema_renaming(installation, question, expected):
    catalog = Catalog(installation["app"])
    dataset = catalog.create(
        "tenant-a",
        "业务档案_" + expected.replace(" ", "_"),
        [],
        primary_key=["事件编号"],
        columns=[{"name": "事件编号", "type": "integer"}, {"name": "所属区域", "type": "text"}],
    )
    relation = f'sdd_data."{dataset["table_name"]}"'
    with installation["owner"].engine.begin() as conn:
        conn.exec_driver_sql(
            f"INSERT INTO {relation} SELECT n, CASE WHEN n=1024 THEN '云岚中心' "
            "WHEN n=1023 THEN 'Amber Quay' ELSE '其他' END FROM generate_series(1,1024) n"
        )
        conn.exec_driver_sql(f'CREATE INDEX ON {relation} ("所属区域")')
        conn.exec_driver_sql(f"ANALYZE {relation}")
    collector = PlanningSamples(catalog)
    evidence = collector.column("tenant-a", dataset, "所属区域").evidence("所属区域")
    assert expected not in evidence["exact_values"] and not evidence["complete"]
    found = collector.matches("tenant-a", dataset, "所属区域", question)
    assert found["output_state"] == "VALUE" and found["operation_state"] == "SUCCEEDED"
    assert expected in found["values"]
