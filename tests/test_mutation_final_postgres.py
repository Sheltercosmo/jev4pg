"""Fresh transaction cases first run after the reviewed-write implementation freeze."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
import os

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from test_deployment_postgres import installation as installation
from sdd.generic.catalog import Catalog
from sdd.generic.sql import SQLService
from sdd.generic import schema


pytestmark = pytest.mark.skipif(
    not os.getenv("SDD_TEST_ADMIN_URL"), reason="Dedicated PostgreSQL server required"
)


def test_boolean_date_key_and_simultaneous_assignment(installation):
    catalog = Catalog(installation["app"])
    catalog.create(
        "tenant-a",
        "switches",
        [
            {"enabled": False, "day": "2026-01-02", "left_value": 4, "right_value": 9},
            {"enabled": True, "day": "2026-01-02", "left_value": 6, "right_value": 8},
        ],
        primary_key=["enabled", "day"],
    )
    sql = SQLService(installation["app"])
    preview = sql.execute(
        "tenant-a",
        """UPDATE switches AS s
        SET left_value=s.right_value, right_value=s.left_value
        WHERE s.enabled=FALSE AND s.day='2026-01-02'""",
        actor="r",
    )
    assert preview["changes_sample"] == [{"left_value": 9, "right_value": 4}]
    sql.commit("tenant-a", preview["preview_token"], "r")
    assert sql.execute("tenant-a", "SELECT left_value,right_value FROM switches ORDER BY enabled")[
        "result"
    ] == [
        {"left_value": 9, "right_value": 4},
        {"left_value": 6, "right_value": 8},
    ]


def test_same_logical_name_different_tenant_and_null_assignment(installation):
    catalog = Catalog(installation["app"])
    for tenant, value in (("tenant-a", "甲"), ("tenant-b", "乙")):
        catalog.create(tenant, "工作单", [{"标识": 1, "记录": value}], primary_key=["标识"])
    sql = SQLService(installation["app"])
    query = 'UPDATE "工作单" SET "记录"=NULL WHERE "标识"=1'
    preview = sql.execute("tenant-a", query, actor="same-name")
    with pytest.raises(ValueError):
        sql.commit("tenant-b", preview["preview_token"], "same-name")
    assert preview["changes_sample"] == [{"记录": None}]
    sql.commit("tenant-a", preview["preview_token"], "same-name")
    assert sql.execute("tenant-a", 'SELECT "记录" FROM "工作单"')["result"] == [{"记录": None}]
    assert sql.execute("tenant-b", 'SELECT "记录" FROM "工作单"')["result"] == [{"记录": "乙"}]


def test_writer_commits_while_review_commit_waits(installation):
    db = installation["app"]
    dataset = Catalog(db).create(
        "tenant-a", "counters", [{"id": 1, "value": 10}], primary_key=["id"]
    )
    relation = f'sdd_data."{dataset["table_name"]}"'
    sql = SQLService(db)
    preview = sql.execute("tenant-a", "UPDATE counters SET value=value+1 WHERE id=1", actor="r")
    waiting = Event()

    def capture(conn, cursor, statement, parameters, context, executemany):
        if dataset["table_name"] in statement and statement.endswith("FOR UPDATE"):
            waiting.set()

    event.listen(db.engine, "before_cursor_execute", capture)
    try:
        with installation["owner"].engine.connect() as writer:
            writer.exec_driver_sql(f"UPDATE {relation} SET value=40 WHERE id=1")
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(sql.commit, "tenant-a", preview["preview_token"], "r")
                try:
                    assert waiting.wait(10)
                finally:
                    writer.commit()
                with pytest.raises(DBAPIError) as conflict:
                    pending.result(timeout=10)
                assert conflict.value.orig.sqlstate == "40001"
        with db.transaction("tenant-a") as conn:
            assert conn.execute(text(f"SELECT value FROM {relation}")).scalar_one() == 40
            assert (
                conn.execute(
                    schema.previews.select().where(schema.previews.c.id == preview["preview_token"])
                )
                .mappings()
                .one()["state"]
                == "pending"
            )
        fresh = sql.execute("tenant-a", "UPDATE counters SET value=value+1 WHERE id=1", actor="r")
        sql.commit("tenant-a", fresh["preview_token"], "r")
        assert sql.execute("tenant-a", "SELECT value FROM counters")["result"] == [{"value": 41}]
    finally:
        event.remove(db.engine, "before_cursor_execute", capture)


def test_replaced_physical_table_requires_new_review(installation):
    db = installation["app"]
    dataset = Catalog(db).create(
        "tenant-a", "rebound", [{"id": 1, "value": 10}], primary_key=["id"]
    )
    sql = SQLService(db)
    preview = sql.execute("tenant-a", "UPDATE rebound SET value=20 WHERE id=1", actor="r")
    name = dataset["table_name"]
    relation, previous = f'sdd_data."{name}"', f'sdd_data."{name}_old"'
    with installation["owner"].engine.begin() as conn:
        conn.exec_driver_sql(f'ALTER TABLE {relation} RENAME TO "{name}_old"')
        conn.exec_driver_sql(f"CREATE TABLE {relation} (LIKE {previous} INCLUDING ALL)")
        conn.exec_driver_sql(f"INSERT INTO {relation} SELECT * FROM {previous}")
        conn.exec_driver_sql(f'GRANT ALL ON {relation} TO "{installation["roles"][0]}"')
        conn.exec_driver_sql(f"ALTER TABLE {relation} ENABLE ROW LEVEL SECURITY")
        conn.exec_driver_sql(f"ALTER TABLE {relation} FORCE ROW LEVEL SECURITY")
        conn.exec_driver_sql(
            f"CREATE POLICY tenant_access ON {relation} USING (current_setting('sdd.tenant',true)='tenant-a')"
        )
    with pytest.raises(ValueError, match="stale"):
        sql.commit("tenant-a", preview["preview_token"], "r")
    with db.transaction("tenant-a") as conn:
        assert conn.execute(text(f"SELECT value FROM {relation}")).scalar_one() == 10
        assert conn.execute(text(f"SELECT value FROM {previous}")).scalar_one() == 10
