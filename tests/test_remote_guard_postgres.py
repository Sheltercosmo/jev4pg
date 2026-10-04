"""Remote guard protocol through real postgres_fdw, without model calls."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import secrets
import time
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from sdd.bootstrap import ensure_login


pytestmark = pytest.mark.skipif(
    not os.getenv("SDD_TEST_ADMIN_URL") or not os.getenv("SDD_TEST_PEER_ADMIN_URL"),
    reason="Two isolated PostgreSQL servers with password authentication required",
)


@pytest.fixture(scope="module")
def remote():
    suffix = uuid.uuid4().hex[:12]
    database = "jev_fdw_" + suffix
    local_role = "jev_fdw_local_" + suffix
    remote_role = "jev_fdw_reader_" + suffix
    other_role = "jev_fdw_other_" + suffix
    password = secrets.token_urlsafe(32)
    bases = [make_url(os.environ[key]) for key in ("SDD_TEST_ADMIN_URL", "SDD_TEST_PEER_ADMIN_URL")]
    fdw_host = os.getenv("SDD_TEST_PEER_FDW_HOST", bases[1].host)
    fdw_port = int(os.getenv("SDD_TEST_PEER_FDW_PORT", str(bases[1].port)))
    admins = [
        create_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True) for url in bases
    ]
    owners, readers = [], []
    try:
        for admin in admins:
            with admin.begin() as connection:
                connection.exec_driver_sql(f'CREATE DATABASE "{database}"')
        owners = [create_engine(url.set(database=database), hide_parameters=True) for url in bases]
        with owners[0].begin() as connection:
            ensure_login(connection, local_role, password)
        with owners[1].begin() as connection:
            ensure_login(connection, remote_role, password)
            ensure_login(connection, other_role, password)
            connection.exec_driver_sql("CREATE SCHEMA legacy; CREATE SCHEMA exports")
            connection.exec_driver_sql("""CREATE TABLE legacy.entries (
                id integer PRIMARY KEY, principal name, note text COLLATE "C", amount numeric(38,10));
                ALTER TABLE legacy.entries ENABLE ROW LEVEL SECURITY;
                CREATE POLICY scope ON legacy.entries USING (principal=current_user);
                CREATE TABLE legacy.unkeyed (note text, amount numeric(38,10));
                INSERT INTO legacy.unkeyed VALUES (NULL,NULL),(NULL,NULL),('完成',12.30);
                CREATE FUNCTION legacy.no_rows(text) RETURNS text LANGUAGE plpgsql AS
                $$BEGIN RAISE EXCEPTION 'source rows evaluated'; END$$;
                CREATE VIEW legacy.invoker WITH(security_invoker=true) AS
                    SELECT id, (1/(id-id))::text AS note FROM legacy.entries;
                CREATE VIEW legacy.opaque WITH(security_invoker=true) AS
                    SELECT id, legacy.no_rows(note) AS note FROM legacy.entries;
                CREATE MATERIALIZED VIEW legacy.stored AS SELECT id,note FROM legacy.entries;
                CREATE TABLE legacy.partitioned (id int) PARTITION BY RANGE(id);
                CREATE TABLE legacy.part PARTITION OF legacy.partitioned FOR VALUES FROM (0) TO (10);
                CREATE VIEW legacy.unsafe AS SELECT id,note FROM legacy.entries;
                CREATE VIEW legacy.nested WITH(security_invoker=true) AS SELECT * FROM legacy.unsafe;
                CREATE TABLE legacy.inherited (id int) INHERITS (legacy.unkeyed);
            """)
            connection.execute(
                text(
                    "INSERT INTO legacy.entries VALUES (1,:role,'完成',12.3),(2,:role,'pending',5.25),(3,:other,'private',99)"
                ),
                {"role": remote_role, "other": other_role},
            )
            connection.exec_driver_sql("COMMENT ON COLUMN legacy.entries.note IS '任务说明'")
            for role in (remote_role, other_role):
                connection.exec_driver_sql(f'GRANT USAGE ON SCHEMA legacy,exports TO "{role}"')
                connection.exec_driver_sql(
                    f'GRANT SELECT ON ALL TABLES IN SCHEMA legacy TO "{role}"'
                )
        script = Path("deploy/federation/remote_guard.sql").read_text(encoding="utf-8")
        with owners[1].connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execution_options(no_parameters=True).exec_driver_sql(script)
        with owners[1].begin() as connection:
            for source in (
                "entries",
                "unkeyed",
                "invoker",
                "opaque",
                "stored",
                "partitioned",
                "unsafe",
                "nested",
            ):
                connection.exec_driver_sql(f"""CREATE VIEW exports.{source}_contract
                    WITH(security_invoker=true) AS SELECT * FROM jev_remote.acquire('legacy.{source}'::regclass)""")
            for role in (remote_role, other_role):
                connection.exec_driver_sql(f'GRANT USAGE ON SCHEMA jev_remote TO "{role}"')
                connection.exec_driver_sql(
                    f'GRANT EXECUTE ON FUNCTION jev_remote.acquire(regclass) TO "{role}"'
                )
                connection.exec_driver_sql(f'GRANT SELECT ON jev_remote.active_guards TO "{role}"')
                connection.exec_driver_sql(
                    f'GRANT SELECT ON ALL TABLES IN SCHEMA exports TO "{role}"'
                )
        with owners[0].begin() as connection:
            connection.exec_driver_sql("CREATE EXTENSION postgres_fdw; CREATE SCHEMA remote")
            for server, role in (("source", remote_role), ("other_source", other_role)):
                connection.exec_driver_sql(f"""CREATE SERVER {server} FOREIGN DATA WRAPPER postgres_fdw
                    OPTIONS(host '{fdw_host}',port '{fdw_port}',dbname '{database}',
                        connect_timeout '3',updatable 'false',truncatable 'false',
                        options '-c statement_timeout=10000 -c lock_timeout=3000')""")
                connection.exec_driver_sql(f"""CREATE USER MAPPING FOR "{local_role}" SERVER {server}
                    OPTIONS(user '{role}',password '{password}')""")
                connection.exec_driver_sql(
                    f'GRANT USAGE ON FOREIGN SERVER {server} TO "{local_role}"'
                )
            for source in (
                "entries",
                "unkeyed",
                "invoker",
                "opaque",
                "stored",
                "partitioned",
                "unsafe",
                "nested",
            ):
                connection.exec_driver_sql(f"""CREATE FOREIGN TABLE remote.{source}_contract (contract jsonb,guard jsonb)
                    SERVER source OPTIONS(schema_name 'exports',table_name '{source}_contract')""")
            for name, server in (("guards", "source"), ("other_guards", "other_source")):
                connection.exec_driver_sql(f"""CREATE FOREIGN TABLE remote.{name} (
                    pid integer,key_hi bigint,key_lo bigint,database bigint,role bigint)
                    SERVER {server} OPTIONS(schema_name 'jev_remote',table_name 'active_guards')""")
            connection.exec_driver_sql("""CREATE FOREIGN TABLE remote.entries (
                id integer,principal name,note text COLLATE "C",amount numeric(38,10))
                SERVER source OPTIONS(schema_name 'legacy',table_name 'entries')""")
            connection.exec_driver_sql(f'GRANT USAGE ON SCHEMA remote TO "{local_role}"')
            connection.exec_driver_sql(
                f'GRANT SELECT ON ALL TABLES IN SCHEMA remote TO "{local_role}"'
            )
        readers = [
            create_engine(
                bases[0].set(database=database, username=local_role, password=password),
                hide_parameters=True,
            ),
            create_engine(
                bases[1].set(database=database, username=remote_role, password=password),
                hide_parameters=True,
            ),
        ]
        yield {
            "local": readers[0],
            "direct": readers[1],
            "owner": owners[1],
            "local_owner": owners[0],
            "role": remote_role,
            "other_role": other_role,
            "database": database,
        }
    finally:
        for engine in readers + owners:
            engine.dispose()
        for index, admin in enumerate(admins):
            with admin.begin() as connection:
                connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
                for role in [local_role] if index == 0 else [remote_role, other_role]:
                    connection.exec_driver_sql(f'DROP ROLE IF EXISTS "{role}"')
            admin.dispose()


def acquire(connection, source="entries"):
    return (
        connection.exec_driver_sql(f"SELECT contract,guard FROM remote.{source}_contract")
        .mappings()
        .one()
    )


def proof(connection, guard, table="guards"):
    return (
        connection.execute(
            text(f"""SELECT count(*) FROM remote.{table}
        WHERE pid=:pid AND key_hi=:key_hi AND key_lo=:key_lo AND database=:database AND role=:role"""),
            guard,
        ).scalar_one()
        == 1
    )


def wait_for_lock(owner, pid):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with owner.connect() as connection:
            if connection.execute(
                text("SELECT EXISTS(SELECT FROM pg_locks WHERE pid=:pid AND NOT granted)"),
                {"pid": pid},
            ).scalar_one():
                return
        time.sleep(0.02)
    raise AssertionError("Expected source lock wait was not observed")


def test_metadata_and_mapped_principal(remote):
    with remote["local"].begin() as guard, remote["local"].begin() as main:
        pinned = acquire(guard)
        current = acquire(main)
        assert current["contract"] == pinned["contract"]
        assert current["guard"]["pid"] != pinned["guard"]["pid"]
        assert proof(main, pinned["guard"])
        assert not proof(main, pinned["guard"], "other_guards")
        assert current["contract"]["constraints"][0]["columns"] == [1]
        assert current["contract"]["columns"][2]["description"] == "任务说明"
        main.exec_driver_sql("SET LOCAL sdd.tenant='another-tenant'")
        assert main.exec_driver_sql(
            "SELECT id FROM remote.entries ORDER BY id"
        ).scalars().all() == [1, 2]


@pytest.mark.parametrize("source", ["invoker", "stored", "partitioned", "unkeyed"])
def test_metadata_does_not_evaluate_rows(remote, source):
    with remote["local"].begin() as guard, remote["local"].begin() as main:
        pinned = acquire(guard, source)
        current = acquire(main, source)
        assert proof(main, pinned["guard"])
        assert current["contract"] == pinned["contract"]


@pytest.mark.parametrize("source", ["unsafe", "nested", "opaque"])
def test_unsupported_dependency_contracts_fail(remote, source):
    with remote["local"].begin() as connection:
        with pytest.raises(DBAPIError, match="invoker|dependencies"):
            acquire(connection, source)


def test_guard_proof_expires_with_transaction(remote):
    with remote["local"].begin() as guard:
        pinned = acquire(guard)
    with remote["local"].begin() as main:
        acquire(main)
        assert not proof(main, pinned["guard"])


def test_guard_disconnect_invalidates_proof(remote):
    guard = remote["local"].connect()
    try:
        pinned = acquire(guard)
        with remote["owner"].begin() as owner:
            owner.execute(text("SELECT pg_terminate_backend(:pid)"), pinned["guard"])
        with remote["local"].begin() as main:
            acquire(main)
            assert not proof(main, pinned["guard"])
    finally:
        guard.invalidate()
        guard.close()


def test_main_keeps_source_locked_after_guard_ends(remote):
    with remote["local"].begin() as main:
        with remote["local"].begin() as guard:
            pinned = acquire(guard)
            acquire(main)
            assert proof(main, pinned["guard"])
        with remote["owner"].begin() as writer:
            writer.exec_driver_sql("SET LOCAL lock_timeout='100ms'")
            with pytest.raises(DBAPIError, match="lock timeout"):
                writer.exec_driver_sql("TRUNCATE legacy.entries")
        assert main.exec_driver_sql("SELECT COUNT(*) FROM remote.entries").scalar_one() == 2


def test_remote_snapshot_is_repeatable(remote):
    with remote["local"].begin() as guard, remote["local"].begin() as main:
        pinned = acquire(guard)
        acquire(main)
        assert proof(main, pinned["guard"])
        before = main.exec_driver_sql("SELECT amount FROM remote.entries WHERE id=1").scalar_one()
        with remote["owner"].begin() as writer:
            writer.exec_driver_sql("UPDATE legacy.entries SET amount=42 WHERE id=1")
        assert (
            main.exec_driver_sql("SELECT amount FROM remote.entries WHERE id=1").scalar_one()
            == before
        )
    with remote["owner"].begin() as writer:
        writer.exec_driver_sql("UPDATE legacy.entries SET amount=12.3 WHERE id=1")


@pytest.mark.parametrize(
    "ddl",
    [
        "ALTER SCHEMA legacy RENAME TO moved",
        "ALTER TABLE legacy.entries ADD COLUMN extra text",
        "ALTER TABLE legacy.unkeyed RENAME COLUMN note TO renamed",
        "ALTER TABLE legacy.inherited NO INHERIT legacy.unkeyed",
        "CREATE OR REPLACE VIEW legacy.invoker WITH(security_invoker=true) AS SELECT id,note FROM legacy.entries",
    ],
)
def test_schema_gate_blocks_source_redirection_and_population_changes(remote, ddl):
    with remote["local"].begin() as guard, remote["local"].begin() as main:
        pinned = acquire(guard)
        acquire(main)
        assert proof(main, pinned["guard"])
        with remote["owner"].begin() as writer:
            writer.exec_driver_sql("SET LOCAL lock_timeout='100ms'")
            with pytest.raises(DBAPIError, match="lock timeout"):
                writer.exec_driver_sql(ddl)


def test_queued_ddl_does_not_deadlock_guard_handoff(remote):
    with remote["owner"].connect() as writer, ThreadPoolExecutor(max_workers=1) as executor:
        pid = writer.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
        guard = remote["local"].connect()
        pending = None
        try:
            acquire(guard)
            pending = executor.submit(
                writer.exec_driver_sql, "COMMENT ON TABLE legacy.entries IS 'queued'"
            )
            wait_for_lock(remote["owner"], pid)
            started = time.monotonic()
            with remote["local"].begin() as main:
                with pytest.raises(DBAPIError, match="schema maintenance is pending"):
                    acquire(main)
            assert time.monotonic() - started < 2
        finally:
            guard.close()
            if pending:
                pending.result(timeout=5)
            writer.rollback()


def test_schema_gate_is_shared_by_concurrent_readers(remote):
    with remote["local"].begin() as first, remote["local"].begin() as second:
        one = acquire(first)
        two = acquire(second)
        assert proof(first, two["guard"]) and proof(second, one["guard"])
        assert first.exec_driver_sql("SELECT COUNT(*) FROM remote.entries").scalar_one() == 2
        assert second.exec_driver_sql("SELECT COUNT(*) FROM remote.entries").scalar_one() == 2


def test_disabled_schema_gate_rejects_new_readers(remote):
    with remote["owner"].begin() as owner:
        owner.exec_driver_sql("ALTER EVENT TRIGGER jev_remote_schema_gate DISABLE")
    try:
        with remote["local"].begin() as connection:
            with pytest.raises(DBAPIError, match="enabled ALWAYS"):
                acquire(connection)
    finally:
        with remote["owner"].begin() as owner:
            owner.exec_driver_sql("ALTER EVENT TRIGGER jev_remote_schema_gate ENABLE ALWAYS")


def test_remote_permission_revoke_is_authoritative(remote):
    with remote["owner"].begin() as owner:
        owner.exec_driver_sql(f'REVOKE SELECT ON legacy.entries FROM "{remote["role"]}"')
    try:
        with remote["local"].begin() as connection:
            with pytest.raises(DBAPIError, match="permission denied"):
                acquire(connection)
    finally:
        with remote["owner"].begin() as owner:
            owner.exec_driver_sql(f'GRANT SELECT ON legacy.entries TO "{remote["role"]}"')


def test_unprotected_fdw_snapshot_demonstrates_truncate_anomaly(remote):
    """The failure the two-transaction protocol must prevent."""
    with remote["owner"].begin() as owner:
        owner.exec_driver_sql(
            "CREATE TABLE legacy.race (id int); INSERT INTO legacy.race VALUES(7)"
        )
    with remote["local_owner"].begin() as owner:
        owner.exec_driver_sql("""CREATE FOREIGN TABLE remote.race (id int)
            SERVER source OPTIONS(schema_name 'legacy',table_name 'race')""")
        owner.exec_driver_sql("GRANT SELECT ON remote.race TO PUBLIC")
    with remote["owner"].begin() as owner:
        owner.exec_driver_sql(f'GRANT SELECT ON legacy.race TO "{remote["role"]}"')
    with remote["local"].begin() as main:
        main.exec_driver_sql("SELECT count(*) FROM remote.guards").scalar_one()
        with remote["owner"].begin() as writer:
            writer.exec_driver_sql("TRUNCATE legacy.race; INSERT INTO legacy.race VALUES (8)")
        assert main.exec_driver_sql("SELECT id FROM remote.race").scalars().all() == []


def test_wrong_server_cannot_prove_remote_guard(remote):
    with remote["local"].begin() as guard:
        pinned = acquire(guard)
        with remote["local_owner"].begin() as wrong:
            assert (
                wrong.execute(
                    text("""SELECT count(*) FROM pg_locks
                WHERE pid=:pid AND classid=:key_hi AND objid=:key_lo AND objsubid=2
                  AND database=:database AND granted"""),
                    pinned["guard"],
                ).scalar_one()
                == 0
            )


def test_expression_dependencies_remain_unknown_until_reviewed(remote):
    with remote["local"].begin() as connection:
        assert acquire(connection, "entries")["contract"]["dependency_state"] == "UNKNOWN"
        assert acquire(connection, "invoker")["contract"]["dependency_state"] == "UNKNOWN"
        assert acquire(connection, "partitioned")["contract"]["dependency_state"] == "VALUE"
        policies = acquire(connection, "entries")["contract"]["relations"][0]["policies"]
        assert policies[0]["name"] == "scope" and "CURRENT_USER" in policies[0]["using"]


def expose(remote, source, alias, columns):
    """Provision a new owner-approved source for a qualification case."""
    with remote["owner"].begin() as owner:
        owner.exec_driver_sql(f"""CREATE VIEW exports.{alias}_contract
            WITH(security_invoker=true) AS SELECT * FROM jev_remote.acquire('{source}'::regclass)""")
        owner.exec_driver_sql(
            f'GRANT SELECT ON {source},exports.{alias}_contract TO "{remote["role"]}"'
        )
    with remote["local_owner"].begin() as owner:
        owner.exec_driver_sql(f"""CREATE FOREIGN TABLE remote.{alias}_contract(contract jsonb,guard jsonb)
            SERVER source OPTIONS(schema_name 'exports',table_name '{alias}_contract')""")
        schema, table = source.replace('"', "").split(".")
        owner.exec_driver_sql(f"""CREATE FOREIGN TABLE remote.{alias} ({columns})
            SERVER source OPTIONS(schema_name '{schema}',table_name '{table}')""")
        owner.exec_driver_sql(f"GRANT SELECT ON remote.{alias},remote.{alias}_contract TO PUBLIC")


def test_schema_rename_unicode_duplicates_nulls_and_empty_population(remote):
    with remote["owner"].begin() as owner:
        owner.exec_driver_sql("""CREATE TABLE legacy.\"记录\" (\"说明\" text COLLATE "C",\"数额\" numeric(38,10));
            INSERT INTO legacy.\"记录\" VALUES(NULL,NULL),(NULL,NULL),('完成',123456789.1234567890)""")
    expose(remote, 'legacy."记录"', "renamed", '"说明" text COLLATE "C","数额" numeric(38,10)')
    with remote["local"].begin() as guard, remote["local"].begin() as main:
        pinned = acquire(guard, "renamed")
        current = acquire(main, "renamed")
        assert proof(main, pinned["guard"])
        assert current["contract"]["dependency_state"] == "VALUE"
        assert current["contract"]["constraints"] == []
        rows = main.exec_driver_sql(
            'SELECT "说明","数额" FROM remote.renamed ORDER BY "数额" NULLS FIRST'
        ).all()
        assert rows[:2] == [(None, None), (None, None)]
        assert str(rows[2][1]) == "123456789.1234567890"
    with remote["owner"].begin() as owner:
        owner.exec_driver_sql('TRUNCATE legacy."记录"')
    with remote["local"].begin() as guard, remote["local"].begin() as main:
        pinned = acquire(guard, "renamed")
        current = acquire(main, "renamed")
        assert proof(main, pinned["guard"])
        assert current["contract"] == pinned["contract"]
        assert main.exec_driver_sql("SELECT count(*) FROM remote.renamed").scalar_one() == 0


def test_builtin_dynamic_sql_requires_review_without_execution(remote):
    with remote["owner"].begin() as owner:
        owner.exec_driver_sql("""CREATE VIEW legacy.dynamic WITH(security_invoker=true) AS
            SELECT query_to_xml('SELECT 1/0',false,false,'') AS value""")
    expose(remote, "legacy.dynamic", "dynamic", "value xml")
    with remote["local"].begin() as connection:
        contract = acquire(connection, "dynamic")["contract"]
        assert contract["dependency_state"] == "UNKNOWN"
        assert "query_to_xml" in contract["relations"][0]["view_sql"]


def test_immutable_user_function_rejected_before_constant_folding(remote):
    with remote["owner"].begin() as owner:
        owner.exec_driver_sql("""CREATE FUNCTION legacy.folded(text) RETURNS text LANGUAGE plpgsql IMMUTABLE AS
            $$BEGIN RAISE EXCEPTION 'constant expression executed'; END$$;
            CREATE VIEW legacy.folded_view WITH(security_invoker=true) AS SELECT legacy.folded('x') AS value""")
    expose(remote, "legacy.folded_view", "folded", "value text")
    with remote["local"].begin() as connection:
        with pytest.raises(DBAPIError, match="declared dependencies"):
            acquire(connection, "folded")


def test_first_inheritance_child_cannot_enter_active_population(remote):
    with remote["owner"].begin() as owner:
        owner.exec_driver_sql("""CREATE TABLE legacy.parent_new (id int);
            INSERT INTO legacy.parent_new VALUES(1);
            CREATE TABLE legacy.child_new (id int); INSERT INTO legacy.child_new VALUES(9)""")
    expose(remote, "legacy.parent_new", "parent_new", "id int")
    with remote["local"].begin() as guard, remote["local"].begin() as main:
        pinned = acquire(guard, "parent_new")
        acquire(main, "parent_new")
        assert proof(main, pinned["guard"])
        with remote["owner"].begin() as writer:
            writer.exec_driver_sql("SET LOCAL lock_timeout='100ms'")
            with pytest.raises(DBAPIError, match="lock timeout"):
                writer.exec_driver_sql("ALTER TABLE legacy.child_new INHERIT legacy.parent_new")
        assert main.exec_driver_sql("SELECT id FROM remote.parent_new").scalars().all() == [1]


def test_lost_guard_after_main_locks_is_detected(remote):
    guard = remote["local"].connect()
    try:
        pinned = acquire(guard)
        with remote["local"].begin() as main:
            acquire(main)
            with remote["owner"].begin() as owner:
                owner.execute(text("SELECT pg_terminate_backend(:pid)"), pinned["guard"])
            assert not proof(main, pinned["guard"])
            with remote["owner"].begin() as writer:
                writer.exec_driver_sql("SET LOCAL lock_timeout='100ms'")
                with pytest.raises(DBAPIError, match="lock timeout"):
                    writer.exec_driver_sql("TRUNCATE legacy.entries")
    finally:
        guard.invalidate()
        guard.close()
