"""Development contracts for reviewed imports and bounded application reads."""

from decimal import Decimal

import pytest
from sqlalchemy import event, insert

from test_generic_api import api as api
from sdd.db import Database
from sdd.generic.browse import TableBrowser
from sdd.generic.catalog import Catalog, serial
from sdd.generic.csv_import import preview_csv


def test_csv_preserves_identifiers_and_infers_all_rows():
    preview, rows = preview_csv(
        "订单",
        '\ufeff编号;金额;启用;日期;说明\n001;12.300;true;2026-03-01;"第一行\n第二行"\n002;0.2;false;2026-03-02;普通\n',
    )
    assert [column["type"] for column in preview["columns"]] == [
        "text",
        "number",
        "boolean",
        "date",
        "text",
    ]
    assert rows[0]["编号"] == "001" and rows[0]["金额"] == Decimal("12.300")
    assert rows[0]["说明"] == "第一行\n第二行"
    assert preview_csv("late", "v\n" + "123\n" * 30 + "abc\n")[0]["columns"][0]["type"] == "text"


def test_csv_reviewed_types_null_and_precision():
    source = "id,value\n9007199254740993,0.12345678901\n"
    preview, _ = preview_csv("precision", source)
    assert preview["sample"][0]["id"] == "9007199254740993"
    assert preview["columns"][1]["type"] == "text"
    preview["columns"][1]["type"] = "number"
    result, _ = preview_csv("precision", source, columns=preview["columns"])
    assert not result["valid"] and result["errors"][0]["column"] == "value"
    assert preview_csv("nulls", 'v\n""\n')[1] == [{"v": None}]
    assert preview_csv("empty", 'v\n""\n', null_empty=False)[1] == [{"v": ""}]


@pytest.mark.parametrize(
    "source", ["a,a\n1,2", "_sdd_id\n1", "\n", "a,b\n1", 'a\n"unclosed', "a\n1\x00"]
)
def test_csv_rejects_invalid_structure(source):
    with pytest.raises(ValueError):
        preview_csv("invalid", source)


def test_csv_api_requires_review_and_creates_exactly_once(api):
    client, headers, _ = api
    body = {"name": "Identifiers", "content": "code,description\n0007,检查\n0008,done\n"}
    assert (
        client.post(
            "/datasets/csv/preview", headers={"Authorization": "Bearer reader"}, json=body
        ).status_code
        == 403
    )
    preview = client.post("/datasets/csv/preview", headers=headers, json=body).json()
    assert len(client.get("/datasets", headers=headers).json()["datasets"]) == 1
    assert client.post("/datasets/csv", headers=headers, json=body).status_code == 400
    reviewed = {**body, "columns": preview["columns"], "fingerprint": preview["fingerprint"]}
    assert (
        client.post(
            "/datasets/csv",
            headers=headers,
            json={**reviewed, "content": body["content"] + "0009,new\n"},
        ).status_code
        == 400
    )
    created = client.post("/datasets/csv", headers=headers, json=reviewed)
    assert created.status_code == 200, created.text
    assert client.post("/datasets/csv", headers=headers, json=reviewed).status_code == 400
    result = client.post(
        "/data/sql", headers=headers, json={"sql": 'SELECT code FROM "Identifiers" ORDER BY code'}
    ).json()
    assert result["result"] == [{"code": "0007"}, {"code": "0008"}]


@pytest.fixture
def browser(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "browse.db"))
    db.initialize()
    catalog = Catalog(db)
    dataset = catalog.create(
        "a",
        "事项",
        [
            {
                "区域": group,
                "序号": index,
                "说明": None if index == 2 else "a_b" if index == 3 else "正常",
                "数值": index,
            }
            for group in ["乙", "甲"]
            for index in range(1, 8)
        ],
        primary_key=["区域", "序号"],
    )
    yield db, catalog, dataset, TableBrowser(db)
    db.engine.dispose()


def test_keyset_composite_projection_and_no_snapshot_copy(browser, monkeypatch):
    db, _, dataset, service = browser
    monkeypatch.setattr(
        Catalog, "rows", lambda *a, **k: pytest.fail("Application pages must not load snapshots")
    )
    statements = []
    event.listen(
        db.engine, "before_cursor_execute", lambda c, cu, sql, p, ctx, many: statements.append(sql)
    )
    after, found = None, []
    while True:
        page = service.scan("a", dataset["id"], columns=["序号"], after=after, limit=3)
        found.extend(page["result"])
        if not page["has_more"]:
            break
        assert page["next_after"] and len(page["next_after"]) == 2
        after = page["next_after"]
    assert [row["序号"] for row in found] == list(range(1, 8)) * 2
    assert not any("count(" in sql.lower() for sql in statements)
    assert all(set(row) == {"序号"} for row in found)


def test_live_cursor_filter_and_inserts(browser):
    db, catalog, dataset, service = browser
    args = {"filters": [{"column": "序号", "op": "gte", "value": "5"}], "limit": 2}
    page = service.scan("a", dataset["id"], **args)
    with db.transaction("a") as conn:
        conn.execute(
            insert(catalog.table(dataset, conn)), {"区域": "乙", "序号": 8, "说明": "新", "数值": 8}
        )
    next_page = service.scan("a", dataset["id"], after=page["next_after"], **args)
    assert [row["序号"] for row in next_page["result"]] == [7, 8]
    escaped = service.scan(
        "a", dataset["id"], filters=[{"column": "说明", "op": "prefix", "value": "a_"}]
    )
    assert len(escaped["result"]) == 2
    nulls = service.scan("a", dataset["id"], filters=[{"column": "说明", "op": "is_null"}])
    assert len(nulls["result"]) == 2


def test_page_byte_boundary_does_not_skip_rows(browser, monkeypatch):
    _, _, dataset, service = browser
    monkeypatch.setattr("sdd.generic.browse.PAGE_BYTES", 150)
    found, after = [], None
    while True:
        page = service.scan("a", dataset["id"], after=after)
        found.extend((row["区域"], row["序号"]) for row in page["result"])
        if not page["has_more"]:
            break
        assert page["page_limited_by"] == "bytes"
        after = page["next_after"]
    assert len(found) == len(set(found)) == 14


@pytest.mark.parametrize(
    "settings",
    [
        {"columns": ["hidden"]},
        {"columns": ["数值", "数值"]},
        {"after": [1]},
        {"after": [None, 1]},
        {"filters": [{"column": "数值", "op": "eq", "value": "1 OR 1=1"}]},
        {"filters": [{"column": "序号", "op": "in", "value": []}]},
        {"filters": [{"column": "说明", "op": "prefix", "value": None}]},
    ],
)
def test_page_rejects_invalid_values(browser, settings):
    _, _, dataset, service = browser
    with pytest.raises((ValueError, ArithmeticError)):
        service.scan("a", dataset["id"], **settings)


def test_scan_api_scope_and_empty_sql_headers(api):
    client, headers, identity = api
    route = f"/datasets/{identity}/scan"
    assert client.post(route, headers={"Authorization": "Bearer other"}, json={}).status_code == 400
    page = client.post(route, headers={"Authorization": "Bearer reader"}, json={"limit": 1})
    assert page.status_code == 200 and page.json()["result"] == [{"样本": 1, "数值": 2}]
    assert client.post(route, headers=headers, json={"limit": 1001}).status_code == 422
    empty = client.post(
        "/data/sql", headers=headers, json={"sql": 'SELECT "样本" AS sample FROM "测量" WHERE 1=0'}
    ).json()
    assert empty["result"] == [] and empty["manifest"]["result_columns"] == ["sample"]
    assert serial({"big": 2**63 - 1, "flag": True}) == {"big": 2**63 - 1, "flag": True}
    large = client.post(
        "/data/sql",
        headers=headers,
        json={"sql": 'SELECT 9007199254740993 AS exact_integer FROM "测量"'},
    ).json()
    assert large["result"] == [{"exact_integer": "9007199254740993"}]


def test_snapshot_limit_is_shared_before_reading_more_tables(monkeypatch):
    from sdd.generic.sql import SQLService

    service = SQLService(Database("sqlite:///:memory:"))
    limits = []

    def rows(tenant, dataset, conn, limit):
        limits.append(limit)
        return [{}] * min(40000, limit)

    monkeypatch.setattr(service.catalog, "rows", rows)
    with pytest.raises(ValueError, match="50,000"):
        service.snapshots("a", [{"id": "one"}, {"id": "two"}, {"id": "three"}])
    assert limits == [50001, 10001]


def test_boolean_primary_key_cursor(browser):
    _, catalog, _, service = browser
    dataset = catalog.create("a", "switches", [{"key": False}, {"key": True}], primary_key=["key"])
    first = service.scan("a", dataset["id"], limit=1)
    assert first["next_after"] == [False]
    assert service.scan("a", dataset["id"], after=first["next_after"])["result"] == [{"key": True}]
