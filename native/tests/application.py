"""Exercise the application compiler against the installed native extension."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import URL, event, text
from sqlalchemy.exc import DBAPIError

from sdd.bootstrap import migrate
from sdd.db import Database
from sdd.generic.catalog import Catalog
from sdd.generic.sql import SQLService
from sdd.generic.native_admission import NativeAdmission
from sdd.deployment import check_database


def verify_application(connection, observations):
    connection.execute("CREATE ROLE native_application LOGIN")
    admin = URL.create(
        "postgresql+psycopg",
        username=connection.info.user,
        host=connection.info.host,
        port=connection.info.port,
        database=connection.info.dbname,
    )
    migrate(admin, runtime_role="native_application", native_interface=True)
    db = Database(admin.set(username="native_application"))
    catalog = Catalog(db)
    sql = SQLService(db, semantic_engine="native")

    def no_snapshot(*args, **kwargs):
        raise AssertionError("Native execution must not fetch a source population into Python")

    sql.snapshots = no_snapshot
    sql.catalog.rows = no_snapshot
    tenant = "native-application-test"
    checks = []
    try:
        previous_engine = os.environ.get("SDD_SEMANTIC_ENGINE")
        os.environ["SDD_SEMANTIC_ENGINE"] = "native"
        try:
            assert check_database(db)["database"] == "ready"
            connection.execute(
                "REVOKE EXECUTE ON FUNCTION jev_native.scan(text,jsonb,jsonb) FROM native_application"
            )
            try:
                check_database(db)
            except ValueError as error:
                assert "migrate --native-interface" in str(error)
            else:
                raise AssertionError("Readiness accepted missing native privileges")
        finally:
            connection.execute(
                "GRANT EXECUTE ON FUNCTION jev_native.scan(text,jsonb,jsonb) TO native_application"
            )
            if previous_engine is None:
                os.environ.pop("SDD_SEMANTIC_ENGINE")
            else:
                os.environ["SDD_SEMANTIC_ENGINE"] = previous_engine
        checks.append("native migration and readiness enforce restricted runtime grants")
        for table_name, key_name, note_name in [
            ("work_items", "id", "note"),
            ("事项", "编号", "说明"),
            ("renamed_source", "key_value", "description"),
        ]:
            records = [
                {key_name: 1, note_name: "完成", "p": 0.95},
                {key_name: 2, note_name: "pending", "p": 0.05},
                {key_name: 3, note_name: "O'Reilly 100% complete", "p": 0.95},
            ]
            dataset = catalog.create(tenant, table_name, records, primary_key=[key_name])
            table, key, note = f'"{table_name}"', f'"{key_name}"', f'"{note_name}"'
            for definition in [
                "The task is complete.",
                "Has the work finished?",
                "任务是否已经完成？",
            ]:
                start = len(observations)
                result = sql.execute(
                    tenant,
                    f"SELECT COUNT(*) AS n FROM {table} WHERE SEMANTIC({note},'{definition}')",
                )
                assert result["result"] == [{"n": 2}], result
                assert result["manifest"]["execution_backend"] == "rust_postgresql"
                assert result["manifest"]["complete"] is True
                assert result["manifest"]["semantic_coverage"]["requests"] == 3
                assert len(observations) == start + 3
            start = len(observations)
            result = sql.execute(
                tenant, f"SELECT {key} FROM {table} WHERE {key}=1 AND SEMANTIC({note},'Complete?')"
            )
            assert result["result"] == [{key_name: 1}]
            assert len(observations) == start + 1
            start = len(observations)
            result = sql.execute(
                tenant,
                f"SELECT {key} FROM {table} WHERE {note}='O''Reilly 100% complete' AND SEMANTIC({note},'Complete?')",
            )
            assert result["result"] == [{key_name: 3}]
            assert len(observations) == start + 1
            start = len(observations)
            result = sql.execute(
                tenant,
                f"SELECT SEMANTIC({note},'Complete?') AS a,SEMANTIC({note},'完成了吗？') AS b FROM {table} WHERE {key}=1",
            )
            assert result["result"] == [{"a": True, "b": True}]
            assert len(observations) == start + 1
            assert len(observations[-1]["questions"]) == 2
            assert all("subject_column" not in q for q in observations[-1]["questions"].values())
            result = sql.execute(
                tenant,
                f"SELECT SEMANTIC({note},'Complete?') AS completed,COUNT(*) AS n FROM {table} GROUP BY SEMANTIC({note},'Complete?') ORDER BY completed",
            )
            assert result["result"] == [{"completed": False, "n": 1}, {"completed": True, "n": 2}]
            result = sql.execute(
                tenant,
                f"WITH selected AS (SELECT {key} FROM {table} WHERE SEMANTIC({note},'Complete?')) SELECT COUNT(*) AS n FROM selected",
            )
            assert result["result"] == [{"n": 2}]
            result = sql.execute(
                tenant,
                f"SELECT a.{key} FROM {table} a WHERE EXISTS (SELECT 1 FROM {table} b WHERE b.{key}=a.{key} AND SEMANTIC(b.{note},'Complete?')) ORDER BY a.{key}",
            )
            assert result["result"] == [{key_name: 1}, {key_name: 3}]
            with db.transaction(tenant + "-other") as cx:
                physical = cx.dialect.identifier_preparer.format_table(catalog.table(dataset, cx))
                start = len(observations)
                count = cx.execute(
                    text("SELECT count(*) FROM jev_native.scan(:source,CAST(:questions AS jsonb))"),
                    {
                        "source": "SELECT * FROM " + physical,
                        "questions": '{"q":{"type":"noul","instructions":"Complete?"}}',
                    },
                ).scalar_one()
                assert count == 0 and len(observations) == start
        checks.append(
            "application semantic reads, grouped questions, CTEs and correlations across renamed English and Chinese schemas"
        )
        checks.append(
            "source filters and escaped literals reach PostgreSQL without Python population snapshots"
        )
        checks.append("restricted application role retains tenant row security during native scans")
        from derived_application import verify_derived_application

        checks.extend(verify_derived_application(sql, catalog, tenant, observations))

        catalog.create(
            tenant,
            "events",
            [
                {"event": 1, "item": 1},
                {"event": 2, "item": 1},
                {"event": 3, "item": 2},
            ],
            primary_key=["event"],
        )
        result = sql.execute(
            tenant,
            "SELECT w.id,e.event FROM work_items w JOIN events e ON w.id=e.item WHERE SEMANTIC(w.note,'Complete?') ORDER BY e.event",
        )
        assert result["result"] == [{"id": 1, "event": 1}, {"id": 1, "event": 2}]
        start = len(observations)
        result = sql.execute(
            tenant,
            "SELECT a.id FROM work_items a JOIN work_items b ON a.id=b.id WHERE SEMANTIC(a.note,'Complete?') AND SEMANTIC(b.note,'Complete?') ORDER BY a.id",
        )
        assert result["result"] == [{"id": 1}, {"id": 3}]
        assert len(observations) == start + 3
        checks.append("semantic joins preserve multiplicity and self joins share source evaluation")

        catalog.create(
            tenant,
            "compound_keys",
            [
                {"account": "A", "revision": 1, "note": "done", "p": 0.95},
                {"account": "A", "revision": 2, "note": "pending", "p": 0.05},
                {"account": "B", "revision": 1, "note": "done", "p": 0.95},
            ],
            primary_key=["account", "revision"],
        )
        result = sql.execute(
            tenant,
            "SELECT account,revision FROM compound_keys WHERE SEMANTIC(note,'Complete?') ORDER BY account,revision",
        )
        assert result["result"] == [
            {"account": "A", "revision": 1},
            {"account": "B", "revision": 1},
        ]
        checks.append("composite source keys preserve row identity")

        changing = catalog.create(
            tenant,
            "changing_source",
            [
                {"id": 1, "note": "original", "p": 0.95},
                {"id": 2, "note": "pending", "p": 0.05},
            ],
            primary_key=["id"],
        )
        changed = False

        def change_after_evaluation(conn, cursor, statement, parameters, context, many):
            nonlocal changed
            if changed or not statement.startswith("CREATE TEMP TABLE"):
                return
            changed = True
            with db.transaction(tenant) as writer:
                source = catalog.table(changing, writer)
                writer.execute(
                    source.update().where(source.c.id == 1).values(note="changed", p=0.05)
                )
                writer.execute(source.delete().where(source.c.id == 2))
                writer.execute(source.insert().values(id=3, note="new", p=0.95))

        event.listen(db.engine, "after_cursor_execute", change_after_evaluation)
        try:
            result = sql.execute(
                tenant,
                "SELECT id,note FROM changing_source WHERE SEMANTIC(note,'Complete?') ORDER BY id",
            )
        finally:
            event.remove(db.engine, "after_cursor_execute", change_after_evaluation)
        assert changed and result["result"] == [{"id": 1, "note": "original"}]
        assert sql.execute(tenant, "SELECT id,note FROM changing_source ORDER BY id")["result"] == [
            {"id": 1, "note": "changed"},
            {"id": 3, "note": "new"},
        ]
        checks.append(
            "native evaluation and relational consumption share one snapshot across concurrent writes"
        )

        catalog.create(
            tenant,
            "missing_notes",
            [{"id": 1, "note": None, "other": "完成", "p": "0.95"}],
            columns=[
                {"name": "id", "type": "integer"},
                {"name": "note", "type": "text"},
                {"name": "other", "type": "text"},
                {"name": "p", "type": "number"},
            ],
            primary_key=["id"],
        )
        start = len(observations)
        result = sql.execute(
            tenant,
            "SELECT SEMANTIC(note,'Complete?') AS missing,SEMANTIC(other,'Complete?') AS present FROM missing_notes",
        )
        assert result["result"] == [{"missing": None, "present": True}]
        assert len(observations) == start + 1 and len(observations[-1]["questions"]) == 1
        coverage = result["manifest"]["semantic_coverage"]
        assert coverage["not_evaluated"] == 1 and coverage["unknown"] == 0
        start = len(observations)
        result = sql.execute(
            tenant, "SELECT SEMANTIC(note,'Complete?') AS missing FROM missing_notes"
        )
        assert result["result"] == [{"missing": None}] and len(observations) == start
        assert not result["manifest"]["complete"]
        checks.append("NULL subject skips only dependent questions and remains NOT_EVALUATED")

        start = len(observations)
        result = sql.execute(
            tenant,
            "SELECT SEMANTIC(a.note,'Complete?') AS a,SEMANTIC(b.description,'Complete?') AS b FROM work_items a JOIN renamed_source b ON a.id=b.key_value",
            max_evaluations=2,
        )
        assert len(observations) == start + 2
        assert result["manifest"]["semantic_coverage"]["new_evaluations"] == 2
        assert result["manifest"]["semantic_coverage"]["budget_skipped"] == 4
        assert result["manifest"]["native_source_populations"] == 2
        assert result["manifest"]["native_scheduler"] == "shared_round_robin"
        assert (
            sum(
                step["sql"].startswith("CREATE TEMP TABLE")
                for step in result["manifest"]["execution_steps"]
            )
            == 1
        )
        with db.transaction(tenant) as cx:
            assert (
                cx.execute(
                    text("SELECT requests FROM native_query_admissions WHERE id=:id"),
                    {"id": result["manifest"]["admission_id"]},
                ).scalar_one()
                == 2
            )
        assert not result["manifest"]["complete"]
        for query in [
            "SELECT COUNT(*) AS n FROM work_items WHERE SEMANTIC(note,'Complete?')",
            "SELECT id FROM work_items WHERE NOT EXISTS (SELECT 1 FROM renamed_source WHERE SEMANTIC(description,'Complete?'))",
            "SELECT id FROM work_items EXCEPT SELECT id FROM work_items WHERE SEMANTIC(note,'Complete?')",
            "SELECT id FROM work_items WHERE COALESCE(SEMANTIC(note,'Complete?'),FALSE)=FALSE",
            "SELECT id FROM work_items WHERE SEMANTIC(note,'Complete?') IS NOT TRUE",
            "SELECT id FROM work_items WHERE SEMANTIC(note,'Complete?') ORDER BY id LIMIT 1",
            "SELECT CASE WHEN SEMANTIC(note,'Complete?') THEN 1 ELSE 0 END AS decision FROM work_items",
            "SELECT id FROM work_items WHERE SEMANTIC(note,'Complete?') IS NULL",
            "SELECT id FROM work_items WHERE SEMANTIC(note,'Complete?') INTERSECT SELECT id FROM work_items",
        ]:
            try:
                sql.execute(tenant, query, max_evaluations=0)
            except ValueError as error:
                assert "Unresolved native decisions" in str(error)
            else:
                raise AssertionError("Incomplete population produced an exact result")
        for predicate, expected in [
            ("SEMANTIC(note,'Complete?')", [{"id": 1}]),
            ("NOT SEMANTIC(note,'Complete?')", []),
            ("SEMANTIC(note,'Complete?')=TRUE", [{"id": 1}]),
            ("NOT (SEMANTIC(note,'Complete?') AND id=2)", [{"id": 1}, {"id": 3}]),
        ]:
            result = sql.execute(
                tenant, "SELECT id FROM work_items WHERE " + predicate, max_evaluations=1
            )
            assert sorted(result["result"], key=lambda row: row["id"]) == expected
            assert not result["manifest"]["complete"]
        start = len(observations)
        result = sql.execute(
            tenant,
            "SELECT COUNT(*) AS n FROM work_items WHERE id<0 AND SEMANTIC(note,'Complete?')",
            max_evaluations=0,
        )
        assert result["result"] == [{"n": 0}] and len(observations) == start
        checks.append(
            "query admission spans dataset scans and incomplete populations cannot produce exact results"
        )
        catalog.create(
            tenant,
            "different_domains",
            [
                {"id": 1, "a": "done", "b": None, "p": 0.95},
                {"id": 2, "a": None, "b": "完成", "p": 0.95},
            ],
            primary_key=["id"],
        )
        start = len(observations)
        result = sql.execute(
            tenant,
            "SELECT COUNT(*) AS n FROM ("
            "SELECT id FROM different_domains WHERE id=1 AND SEMANTIC(a,'Complete?') "
            "UNION ALL SELECT id FROM different_domains WHERE id=2 AND SEMANTIC(b,'完成了吗？')"
            ") selected",
            max_evaluations=2,
        )
        assert result["result"] == [{"n": 2}] and result["manifest"]["complete"]
        assert len(observations) == start + 2
        assert all(len(item["questions"]) == 1 for item in observations[start:])
        assert result["manifest"]["semantic_coverage"]["unresolved"] == 0
        checks.append(
            "question-specific source domains avoid unrelated calls and false incompleteness"
        )
        catalog.create(
            tenant,
            "matching_contexts",
            [
                {"id": 1, "note": "完成", "p": 0.95},
                {"id": 2, "note": "pending", "p": 0.05},
                {"id": 3, "note": "O'Reilly 100% complete", "p": 0.95},
            ],
            primary_key=["id"],
        )
        start = len(observations)
        result = sql.execute(
            tenant,
            "SELECT a.id FROM work_items a JOIN matching_contexts b ON a.id=b.id "
            "WHERE SEMANTIC(a.note,'Complete?') AND SEMANTIC(b.note,'Complete?') ORDER BY a.id",
        )
        assert result["result"] == [{"id": 1}, {"id": 3}]
        assert len(observations) == start + 3
        assert result["manifest"]["semantic_coverage"]["requests"] == 3
        assert result["manifest"]["semantic_coverage"]["resolved"] == 6
        checks.append(
            "application populations share context reuse and settle cumulative usage only once"
        )
        start = len(observations)
        try:
            sql.execute(
                tenant,
                "SELECT w.id FROM work_items AS w(p,note,id) "
                "WHERE w.id=2 AND SEMANTIC(w.note,'Complete?')",
            )
        except ValueError as error:
            assert "column alias lists" in str(error)
        else:
            raise AssertionError("Semantic query accepted unmapped ordinal column aliases")
        assert len(observations) == start
        checks.append("unsupported column alias lineage is rejected before provider dispatch")

        catalog.create(
            tenant, "join_subjects", [{"id": 99, "note": "done", "p": 0.95}], primary_key=["id"]
        )
        for empty in (False, True):
            if empty:
                dataset = catalog.get(tenant, "join_subjects")
                with db.transaction(tenant) as cx:
                    cx.execute(catalog.table(dataset, cx).delete())
            for source, subject in [
                ("work_items a LEFT JOIN join_subjects b ON a.id=b.id", "b.note"),
                ("join_subjects b RIGHT JOIN work_items a ON a.id=b.id", "b.note"),
                ("work_items a FULL JOIN join_subjects b ON a.id=b.id", "a.note"),
                ("work_items a LEFT JOIN (join_subjects b) ON a.id=b.id", "b.note"),
                (
                    "(work_items a LEFT JOIN join_subjects b ON a.id=b.id) JOIN events e ON a.id=e.item",
                    "b.note",
                ),
                (
                    "work_items a JOIN (events e LEFT JOIN join_subjects b ON e.item=b.id) ON a.id=e.item",
                    "b.note",
                ),
            ]:
                start = len(observations)
                try:
                    sql.execute(
                        tenant,
                        f"SELECT a.id FROM {source} WHERE SEMANTIC({subject},'Complete?') IS NOT TRUE",
                    )
                except ValueError as error:
                    assert "NULL-extended" in str(error)
                else:
                    raise AssertionError("Unmatched semantic subject was reported as evaluated")
                assert len(observations) == start
        result = sql.execute(
            tenant,
            "SELECT a.id FROM work_items a LEFT JOIN join_subjects b ON a.id=b.id "
            "WHERE SEMANTIC(a.note,'Complete?') ORDER BY a.id",
        )
        assert result["result"] == [{"id": 1}, {"id": 3}] and result["manifest"]["complete"]
        checks.append(
            "outer-join evaluation rejects NULL-extended aliases and retains preserved aliases"
        )
        for grouping in ("ROLLUP(id,note)", "CUBE(id,note)", "GROUPING SETS ((id,note),())"):
            start = len(observations)
            try:
                sql.execute(
                    tenant,
                    "SELECT id,note,SEMANTIC(note,'Complete?') AS completed FROM work_items "
                    "GROUP BY " + grouping,
                )
            except ValueError as error:
                assert "ordinary GROUP BY" in str(error)
            else:
                raise AssertionError("Subtotal row reused a decision for a different subject")
            assert len(observations) == start
        result = sql.execute(
            tenant,
            "WITH evaluated AS (SELECT id,SEMANTIC(note,'Complete?') AS completed FROM work_items) "
            "SELECT completed,COUNT(*) AS n FROM evaluated GROUP BY ROLLUP(completed) "
            "ORDER BY completed NULLS LAST",
        )
        assert result["result"] == [
            {"completed": False, "n": 1},
            {"completed": True, "n": 2},
            {"completed": None, "n": 3},
        ]
        checks.append("grouping sets require semantic evaluation before synthetic subtotal rows")

        cancel_tenant = "native-cancel-test"
        catalog.create(
            cancel_tenant,
            "slow_source",
            [{"id": 1, "note": "cancel-this-query", "delay": 2, "p": 0.95}],
            primary_key=["id"],
        )
        backend = []

        def capture_backend(conn, cursor, statement, parameters, context, many):
            if statement.startswith("CREATE TEMP TABLE"):
                backend.append(conn.connection.driver_connection.info.backend_pid)

        event.listen(db.engine, "before_cursor_execute", capture_backend)
        start = len(observations)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    sql.execute,
                    cancel_tenant,
                    "SELECT id FROM slow_source WHERE SEMANTIC(note,'Complete?')",
                    max_evaluations=2,
                )
                deadline = time.monotonic() + 10
                while (
                    len(observations) == start and not future.done() and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                assert backend and len(observations) == start + 1
                assert connection.execute("SELECT pg_cancel_backend(%s)", (backend[0],)).fetchone()[
                    0
                ]
                try:
                    future.result(timeout=5)
                except DBAPIError as error:
                    assert "canceling statement" in str(error)
                else:
                    raise AssertionError(
                        "Native application cancellation did not interrupt the scan"
                    )
        finally:
            event.remove(db.engine, "before_cursor_execute", capture_backend)
        with db.transaction(cancel_tenant) as cx:
            row = (
                cx.execute(
                    text(
                        "SELECT reserved,requests,state FROM native_query_admissions WHERE tenant=:tenant"
                    ),
                    {"tenant": cancel_tenant},
                )
                .mappings()
                .one()
            )
            assert dict(row) == {"reserved": 2, "requests": None, "state": "UNCERTAIN"}
            assert (
                cx.execute(
                    text("SELECT calls FROM tenant_daily_usage WHERE tenant=:tenant"),
                    {"tenant": cancel_tenant},
                ).scalar_one()
                == 2
            )
        checks.append(
            "cancellation after real dispatch preserves durable uncertain usage through rollback"
        )
        failure_tenant = "native-late-source-error"
        for table_name in ("small_rows", "large_rows"):
            catalog.create(
                failure_tenant,
                table_name,
                [
                    {
                        "id": index,
                        "note": "x" * 1_000_001
                        if table_name == "large_rows" and index == 17
                        else table_name,
                        "p": 0.95,
                    }
                    for index in range(1, 18)
                ],
                primary_key=["id"],
            )
        start = len(observations)
        try:
            sql.execute(
                failure_tenant,
                "SELECT SEMANTIC(a.note,'Complete?') AS a,SEMANTIC(b.note,'Complete?') AS b "
                "FROM small_rows a JOIN large_rows b ON a.id=b.id",
                max_evaluations=40,
            )
        except DBAPIError as error:
            assert "1 MB context limit" in str(error)
        else:
            raise AssertionError("Native source accepted an oversized context")
        assert len(observations) == start + 32
        with db.transaction(failure_tenant) as cx:
            reservation = cx.execute(
                text(
                    "SELECT reserved,requests,state FROM native_query_admissions WHERE tenant=:tenant"
                ),
                {"tenant": failure_tenant},
            ).one()
            assert tuple(reservation) == (40, None, "UNCERTAIN")
            assert (
                cx.execute(
                    text("SELECT calls FROM tenant_daily_usage WHERE tenant=:tenant"),
                    {"tenant": failure_tenant},
                ).scalar_one()
                == 40
            )
        checks.append(
            "a late error in another source retains the shared allowance after real dispatch"
        )
        previous = os.environ.get("SDD_DAILY_EVALUATIONS")
        os.environ["SDD_DAILY_EVALUATIONS"] = "3"
        clients = [Database(admin.set(username="native_application")) for _ in range(2)]
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                reservations = list(
                    pool.map(
                        lambda client: NativeAdmission.reserve(client, "quota-client", 2), clients
                    )
                )
            assert sorted(item.reserved for item in reservations) == [1, 2]
            for item in reservations:
                item.finish(1)
                item.finish(1)
            with db.transaction("quota-client") as cx:
                assert (
                    cx.execute(
                        text("SELECT calls FROM tenant_daily_usage WHERE tenant='quota-client'")
                    ).scalar_one()
                    == 2
                )
            from sdd.generic.jev import reserve

            reserve(db, "quota-client")
            held = NativeAdmission.reserve(db, "quota-client", 1)
            assert held.reserved == 0
            held.finish(0)
        finally:
            for client in clients:
                client.engine.dispose()
            if previous is None:
                os.environ.pop("SDD_DAILY_EVALUATIONS")
            else:
                os.environ["SDD_DAILY_EVALUATIONS"] = previous
        checks.append(
            "independent clients and legacy calls share one durable daily allowance with idempotent settlement"
        )
        catalog.create(
            tenant, "mutable_rows", [{"id": 1, "amount": 2}], primary_key=["id"], writable=True
        )
        writer = SQLService(db, semantic_engine="native")
        preview = writer.execute(
            tenant, "UPDATE mutable_rows SET amount=3 WHERE id=1", actor="reviewer"
        )
        assert preview["mutation_preview"] and preview["affected_rows"] == 1
        committed = writer.commit(tenant, preview["preview_token"], "reviewer")
        assert committed["manifest"]["committed"]
        assert writer.execute(tenant, "SELECT amount FROM mutable_rows")["result"] == [
            {"amount": 3}
        ]
        checks.append(
            "native semantic configuration preserves ordinary mutation preview and confirmation"
        )
        from catalog_application import verify_catalog_application

        checks.extend(verify_catalog_application(connection, sql, catalog, tenant, observations))
        return checks
    finally:
        db.engine.dispose()
