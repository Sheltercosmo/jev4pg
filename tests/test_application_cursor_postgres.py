"""Previously unseen cursor contracts, now retained as regression cases."""

from datetime import datetime, timedelta
import os
import uuid

import pytest

from test_deployment_postgres import installation as installation
from sdd.generic.browse import TableBrowser
from sdd.generic.catalog import Catalog


pytestmark = pytest.mark.skipif(
    not os.getenv("SDD_TEST_ADMIN_URL"), reason="Dedicated PostgreSQL server required"
)


@pytest.mark.parametrize(
    "names",
    [
        ("telemetry", "moment", "device", "reading", "note"),
        ("设备记录", "时刻", "设备", "读数", "备注"),
    ],
)
def test_uuid_timestamp_composite_pages_under_renaming(installation, names):
    logical, moment, device, reading, note = names
    schema = "cursor_" + uuid.uuid4().hex[:10]
    with installation["owner"].engine.begin() as conn:
        conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        conn.exec_driver_sql(f'''CREATE TABLE "{schema}".records (
            "{moment}" timestamp NOT NULL, "{device}" uuid NOT NULL,
            "{reading}" bigint, "{note}" text, PRIMARY KEY("{moment}","{device}"))''')
        conn.exec_driver_sql(f'''INSERT INTO "{schema}".records
            SELECT '2026-01-01'::timestamp + mod(n,3) * interval '1 hour',
                ('00000000-0000-0000-0000-'||lpad(n::text,12,'0'))::uuid,
                9007199254740992+n,
                CASE WHEN mod(n,7)=0 THEN NULL WHEN mod(n,2)=0 THEN '完成复核' ELSE '待确认' END
            FROM generate_series(1,40) n''')
        conn.exec_driver_sql(f'GRANT USAGE ON SCHEMA "{schema}" TO "{installation["roles"][0]}"')
        conn.exec_driver_sql(f'GRANT SELECT ON "{schema}".records TO "{installation["roles"][0]}"')
    catalog = Catalog(installation["app"])
    dataset = catalog.attach("tenant-a", logical, schema, "records")
    browser = TableBrowser(installation["app"])
    rows, cursor = [], None
    while True:
        page = browser.scan(
            "tenant-a",
            dataset["id"],
            columns=[reading],
            filters=[{"column": note, "op": "prefix", "value": "完成"}],
            after=cursor,
            limit=3,
        )
        rows.extend(page["result"])
        if not page["has_more"]:
            break
        cursor = page["next_after"]
        assert datetime.fromisoformat(cursor[0]).tzinfo is None
        assert isinstance(uuid.UUID(cursor[1]), uuid.UUID)
    expected = sorted(
        [n for n in range(1, 41) if n % 2 == 0 and n % 7],
        key=lambda n: (datetime(2026, 1, 1) + timedelta(hours=n % 3), n),
    )
    assert rows == [{reading: str(9007199254740992 + n)} for n in expected]


def test_exact_decimal_primary_key_and_empty_page(installation):
    catalog = Catalog(installation["app"])
    dataset = catalog.create(
        "tenant-a",
        "decimal_positions",
        [
            {"key": "9876543210987654321.0000000001", "seq": 1, "value": None},
            {"key": "9876543210987654321.0000000002", "seq": 1, "value": "x"},
            {"key": "9876543210987654321.0000000002", "seq": 2, "value": "x"},
        ],
        columns=[
            {"name": "key", "type": "number"},
            {"name": "seq", "type": "integer"},
            {"name": "value", "type": "text"},
        ],
        primary_key=["key", "seq"],
    )
    browser = TableBrowser(installation["app"])
    first = browser.scan("tenant-a", dataset["id"], columns=["value"], limit=1)
    assert first["next_after"] == ["9876543210987654321.0000000001", 1]
    second = browser.scan("tenant-a", dataset["id"], after=first["next_after"], limit=1)
    assert second["result"][0]["key"] == "9876543210987654321.0000000002"
    end = browser.scan("tenant-a", dataset["id"], after=["9876543210987654321.0000000002", 2])
    assert end["result"] == [] and end["next_after"] is None and not end["has_more"]


def test_large_cells_page_by_bytes_without_losing_a_row(installation):
    catalog = Catalog(installation["app"])
    dataset = catalog.create(
        "tenant-a",
        "documents",
        [{"id": n, "body": "字" * 800000} for n in range(1, 4)],
        primary_key=["id"],
    )
    browser = TableBrowser(installation["app"])
    rows, cursor = [], None
    for _ in range(3):
        page = browser.scan("tenant-a", dataset["id"], after=cursor)
        assert len(page["result"]) == 1
        rows.append(page["result"][0]["id"])
        cursor = page["next_after"]
    assert rows == [1, 2, 3] and cursor is None


def test_source_change_blocks_next_page(installation):
    schema = "cursor_" + uuid.uuid4().hex[:10]
    with installation["owner"].engine.begin() as conn:
        conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        conn.exec_driver_sql(f'CREATE TABLE "{schema}".records (id int PRIMARY KEY, body text)')
        conn.exec_driver_sql(f"INSERT INTO \"{schema}\".records VALUES (1,'a'),(2,'b')")
        conn.exec_driver_sql(f'GRANT USAGE ON SCHEMA "{schema}" TO "{installation["roles"][0]}"')
        conn.exec_driver_sql(f'GRANT SELECT ON "{schema}".records TO "{installation["roles"][0]}"')
    catalog, browser = Catalog(installation["app"]), TableBrowser(installation["app"])
    dataset = catalog.attach("tenant-a", "source_recheck", schema, "records")
    first = browser.scan("tenant-a", dataset["id"], limit=1)
    with installation["owner"].engine.begin() as conn:
        conn.exec_driver_sql(f"COMMENT ON COLUMN \"{schema}\".records.body IS 'Changed meaning'")
    with pytest.raises(ValueError, match="schema changed"):
        browser.scan("tenant-a", dataset["id"], after=first["next_after"])
