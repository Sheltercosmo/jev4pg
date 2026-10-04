"""Application-read validation on an isolated, generated million-row relation."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from statistics import median
from time import perf_counter
import tracemalloc

import pytest
from fastapi.testclient import TestClient

from test_deployment_postgres import installation as installation
from sdd.api import create_app
from sdd.execution import Executor
from sdd.generic.browse import TableBrowser, page_query
from sdd.generic.catalog import Catalog
from sdd.generic.source_catalog import source_transaction
from sdd.generic.sql import SQLService


pytestmark = pytest.mark.skipif(
    not os.getenv("SDD_TEST_ADMIN_URL"), reason="Dedicated PostgreSQL server required"
)


@pytest.fixture(scope="module")
def workload(installation):
    env = installation
    with env["owner"].engine.begin() as conn:
        conn.exec_driver_sql("CREATE SCHEMA application_example")
        conn.exec_driver_sql("""CREATE TABLE application_example.events (
            id bigint PRIMARY KEY, owner text NOT NULL, category text NOT NULL,
            amount numeric(38,10), note text, created_at timestamptz NOT NULL)""")
        conn.exec_driver_sql("""INSERT INTO application_example.events
            SELECT n, CASE WHEN mod(n,5)=0 THEN 'tenant-b' ELSE 'tenant-a' END,
                   CASE WHEN mod(n,7)=0 THEN 'warning' ELSE 'normal' END,
                   n::numeric/100, repeat('application event ',8),
                   '2026-01-01'::timestamptz + n * interval '1 second'
            FROM generate_series(1,1000000) n""")
        conn.exec_driver_sql(
            "CREATE INDEX events_filter ON application_example.events(owner,category,id)"
        )
        conn.exec_driver_sql("ALTER TABLE application_example.events ENABLE ROW LEVEL SECURITY")
        conn.exec_driver_sql(
            "CREATE POLICY tenant_scope ON application_example.events USING (owner=current_setting('sdd.tenant',true))"
        )
        conn.exec_driver_sql(f'GRANT USAGE ON SCHEMA application_example TO "{env["roles"][0]}"')
        conn.exec_driver_sql(f'GRANT SELECT ON application_example.events TO "{env["roles"][0]}"')
        conn.exec_driver_sql("ANALYZE application_example.events")
    catalog = Catalog(env["app"])
    dataset = catalog.attach(
        "tenant-a",
        "activity",
        "application_example",
        "events",
        ["id", "category", "amount", "note", "created_at"],
    )
    return {**env, "dataset": dataset, "catalog": catalog, "browser": TableBrowser(env["app"])}


def test_indexed_deep_pages_exact_results_and_rls(workload):
    service, dataset = workload["browser"], workload["dataset"]
    for cursor in (None, [500000], [950000], [999990]):
        page = service.scan(
            "tenant-a",
            dataset["id"],
            columns=["id", "amount"],
            filters=[{"column": "category", "op": "eq", "value": "warning"}],
            after=cursor,
            limit=100,
        )
        expected = [n for n in range((cursor or [0])[0] + 1, 1000001) if n % 5 and n % 7 == 0][:100]
        assert [row["id"] for row in page["result"]] == expected
        assert all(str(row["amount"]).endswith("00000000") for row in page["result"])
    with pytest.raises(ValueError):
        service.scan("tenant-b", dataset["id"])
    with source_transaction(workload["app"], "tenant-a", [dataset]) as conn:
        table = workload["catalog"].table(dataset, conn)
        query, _ = page_query(
            table,
            dataset,
            ["id"],
            [{"column": "category", "op": "eq", "value": "warning"}],
            [950000],
            100,
        )
        compiled = query.compile(dialect=conn.dialect, compile_kwargs={"literal_binds": True})
        plan = conn.exec_driver_sql(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + str(compiled)
        ).scalar_one()[0]
        assert "Index" in json.dumps(plan) and "950000" in json.dumps(plan)
        assert plan["Plan"]["Actual Rows"] == 101


def test_large_aggregate_executes_in_postgresql(workload, monkeypatch):
    monkeypatch.setattr(
        Catalog, "rows", lambda *a, **kw: pytest.fail("No table copy for relational SQL")
    )
    output = SQLService(workload["app"]).execute(
        "tenant-a",
        "SELECT category, COUNT(*) AS n, SUM(amount) AS total FROM activity GROUP BY category ORDER BY category",
    )
    assert sum(row["n"] for row in output["result"]) == 800000
    assert output["manifest"]["source_rows_state"] == "NOT_EVALUATED"
    assert output["manifest"]["execution_backend"] == "postgresql"


def test_reviewed_csv_roundtrip_exact_postgresql_numbers(workload):
    from sdd.generic.csv_import import preview_csv

    preview, rows = preview_csv(
        "exact_import", "id,amount\n9007199254740993,12345678901234567890.1234567890\n"
    )
    catalog = workload["catalog"]
    dataset = catalog.create(
        "tenant-a", "exact_import", rows, columns=preview["columns"], primary_key=["id"]
    )
    output = workload["browser"].scan("tenant-a", dataset["id"])
    assert output["result"] == [
        {"id": "9007199254740993", "amount": "12345678901234567890.1234567890"}
    ]


def test_concurrent_application_requests_and_memory(workload):
    dataset = workload["dataset"]
    client = TestClient(
        create_app(
            Executor(workload["app"], {}),
            tokens={"local-test": {"tenant": "tenant-a", "name": "application", "role": "reader"}},
        )
    )
    route = f"/datasets/{dataset['id']}/scan"

    def request(index):
        start = perf_counter()
        after = index * 20000
        response = client.post(
            route,
            headers={"Authorization": "Bearer local-test"},
            json={"columns": ["id", "category", "amount"], "after": [after], "limit": 100},
        )
        assert response.status_code == 200, response.text
        rows = response.json()["result"]
        assert len(rows) == 100 and rows[0]["id"] > after and all(row["id"] % 5 for row in rows)
        return (perf_counter() - start) * 1000

    request(0)
    sequential = [request(index) for index in (0, 10, 20, 30, 40, 45, 46, 47)]
    start = perf_counter()
    with ThreadPoolExecutor(max_workers=8) as pool:
        latencies = list(pool.map(request, range(40)))
    wall = perf_counter() - start
    tracemalloc.start()
    for _ in range(5):
        workload["browser"].scan(
            "tenant-a", dataset["id"], after=[950000], columns=["id", "category", "amount"]
        )
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 12 * 1024 * 1024
    report = {
        "population": 1000000,
        "tenant_visible": 800000,
        "data": "generated application events",
        "page_rows": 100,
        "sequential_median_ms": round(median(sequential), 2),
        "concurrency": 8,
        "requests": 40,
        "concurrent_median_ms": round(median(latencies), 2),
        "concurrent_p95_ms": round(sorted(latencies)[37], 2),
        "requests_per_second": round(40 / wall, 2),
        "python_traced_peak_mib": round(peak / 1024**2, 2),
        "provider_calls": 0,
        "limitations": "Local warm-cache generated data; traced Python allocations exclude database, driver native memory and process RSS.",
    }
    Path(".runtime").mkdir(exist_ok=True)
    Path(".runtime/application-scale-validation.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report))
