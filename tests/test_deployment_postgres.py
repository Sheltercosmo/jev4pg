"""Fresh installation and real SQL clients; requires an isolated admin server."""

import json
import os
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from sdd.bootstrap import SCHEMA_VERSION, ensure_login, grant_client, migrate
from sdd.db import Database
from sdd.deployment import check_database
from sdd.sql_worker import SQLWorker
from test_operator_runtime import Model

pytestmark = pytest.mark.skipif(
    not os.getenv("SDD_TEST_ADMIN_URL"), reason="Dedicated admin test URL required"
)


@pytest.fixture(scope="module")
def installation():
    base = make_url(os.environ["SDD_TEST_ADMIN_URL"])
    suffix = uuid.uuid4().hex[:12]
    database = "jev_install_" + suffix
    roles = ["jev_" + kind + "_" + suffix for kind in ("app", "alice", "bob")]
    password = secrets.token_urlsafe(32)
    admin = create_engine(base, isolation_level="AUTOCOMMIT", hide_parameters=True)
    databases = []
    with admin.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{database}"')
    url = base.set(database=database)
    owner = Database(url)
    try:
        migrate(url, roles[0], password, sql_interface=True)
        migrate(url, roles[0], sql_interface=True)
        with owner.engine.begin() as connection:
            for role in roles[1:]:
                ensure_login(connection, role, password)
        for role, tenant in zip(roles[1:], ("tenant-a", "tenant-b")):
            grant_client(url, role, tenant, role)
        databases = [Database(url.set(username=role, password=password)) for role in roles]
        yield {
            "owner": owner,
            "app": databases[0],
            "alice": databases[1],
            "bob": databases[2],
            "roles": roles,
            "url": url,
        }
    finally:
        for db in databases + [owner]:
            db.engine.dispose()
        with admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
            for role in roles:
                connection.exec_driver_sql(f'DROP ROLE IF EXISTS "{role}"')
        admin.dispose()


def submit(db, proposition="yes", *, state="record", limits=None, key=None, operator="NOUL"):
    with db.engine.begin() as connection:
        return str(
            connection.execute(
                text(
                    "SELECT jev.submit(:operator, CAST(:args AS jsonb), CAST(:limits AS jsonb), '{}', :key)"
                ),
                {
                    "operator": operator,
                    "args": json.dumps({"state": state, "proposition": proposition}),
                    "limits": json.dumps(limits or {}),
                    "key": key,
                },
            ).scalar_one()
        )


def result(db, job):
    with db.engine.begin() as connection:
        return connection.execute(
            text("SELECT jev.result(CAST(:job AS uuid))"), {"job": job}
        ).scalar_one()


def test_fresh_repeated_install_and_runtime_grants(installation):
    env = installation
    assert check_database(env["app"], sql_interface=True)["schema_version"] == SCHEMA_VERSION
    with pytest.raises(ValueError):
        check_database(env["owner"])
    with pytest.raises(ValueError):
        grant_client(env["url"], env["roles"][0], "tenant", "unsafe")
    with env["owner"].engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname='public' AND c.relkind='r' AND c.relname<>'sdd_schema_version' "
                    "AND (NOT c.relrowsecurity OR NOT c.relforcerowsecurity)"
                )
            ).scalar_one()
            == 0
        )


def test_pending_is_unexecuted_and_only_committed_jobs_are_claimed(installation):
    env = installation
    worker = SQLWorker(env["app"], Model())
    with env["alice"].engine.connect() as connection:
        transaction = connection.begin()
        job = str(
            connection.execute(
                text('SELECT jev.submit(\'NOUL\', \'{"state":"record","proposition":"yes"}\')')
            ).scalar_one()
        )
        assert worker.claim() is None
        transaction.commit()
    pending = result(env["alice"], job)
    assert pending["output_state"] == "NOT_EVALUATED" and pending["value"] is None
    assert worker.work_one()
    assert result(env["alice"], job)["job_state"] == "COMPLETED"


@pytest.mark.parametrize(
    "proposition,limits,expected",
    [
        ("no", {}, "VALUE"),
        ("ambiguous", {}, "UNKNOWN"),
        ("是否需要跟进？", {"max_judgments": 0}, "NOT_EVALUATED"),
        ("Does this need follow-up?", {}, "VALUE"),
    ],
)
def test_value_unknown_and_budget_states(installation, proposition, limits, expected):
    env = installation
    job = submit(env["alice"], proposition, limits=limits, state="用户表示问题仍未解决。")
    worker = SQLWorker(env["app"], Model({"no": 0.05, "ambiguous": 0.5}))
    assert worker.work_one()
    response = result(env["alice"], job)
    observation = next(iter(response["observations"].values()))
    assert observation["output_state"] == expected
    if proposition == "no":
        assert observation["value"] is False
    if expected == "NOT_EVALUATED":
        assert response["operation_state"] == "BLOCKED_BY_BUDGET"


def test_sql_identity_cannot_be_spoofed(installation):
    env = installation
    job = submit(env["alice"], key="identity")
    with pytest.raises(DBAPIError), env["bob"].engine.begin() as connection:
        connection.execute(text("SELECT set_config('sdd.tenant', 'tenant-a', true)"))
        connection.execute(text("SELECT jev.result(CAST(:job AS uuid))"), {"job": job})
    for sql in (
        "SELECT * FROM jev.jobs",
        "SELECT * FROM jev.client_roles",
        "SELECT * FROM public.jev_operator_runs",
        "SELECT jev._claim(gen_random_uuid(),60)",
        f'SET ROLE "{env["roles"][0]}"',
    ):
        with pytest.raises(DBAPIError), env["bob"].engine.begin() as connection:
            connection.exec_driver_sql(sql)
    with env["alice"].engine.begin() as connection:
        assert connection.execute(
            text("SELECT jev.cancel(CAST(:job AS uuid))"), {"job": job}
        ).scalar_one()
    assert result(env["alice"], job)["output_state"] == "NOT_EVALUATED"


def test_idempotency_and_invalid_requests(installation):
    env = installation
    job = submit(env["alice"], key="once")
    assert submit(env["alice"], key="once") == job
    with pytest.raises(DBAPIError):
        submit(env["alice"], "different", key="once")
    with pytest.raises(DBAPIError):
        submit(env["alice"], state="x" * 524288)
    assert SQLWorker(env["app"], Model()).work_one()
    invalid = submit(env["alice"], operator="not_a_function")
    assert SQLWorker(env["app"], Model()).work_one()
    response = result(env["alice"], invalid)
    assert response["job_state"] == "FAILED" and response["value"] is None


def test_reader_cannot_promote_itself_through_operator_dispatch(installation):
    env = installation
    with env["alice"].engine.begin() as connection:
        job = str(
            connection.execute(
                text("SELECT jev.submit('REVIEW', '{\"observations\":[]}')")
            ).scalar_one()
        )
    assert SQLWorker(env["app"], Model()).work_one()
    response = result(env["alice"], job)
    assert response["job_state"] == "FAILED" and response["error_type"] == "PermissionError"


def test_security_definer_ignores_client_search_path(installation):
    env = installation
    with env["alice"].engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TEMP TABLE client_roles(login name, tenant text, actor text, access text)"
        )
        connection.exec_driver_sql(
            "INSERT INTO client_roles VALUES(session_user, 'tenant-b', 'forged', 'reviewer')"
        )
        connection.exec_driver_sql("SET LOCAL search_path=pg_temp,public")
        job = str(
            connection.execute(
                text('SELECT jev.submit(\'NOUL\', \'{"state":"record","proposition":"yes"}\')')
            ).scalar_one()
        )
    worker = SQLWorker(env["app"], Model())
    claimed = worker.claim()
    assert claimed["id"] == job and claimed["tenant"] == "tenant-a" and claimed["role"] == "reader"
    assert worker.finish(
        job, {"output_state": "UNKNOWN", "operation_state": "SUCCEEDED", "value": None}
    )


def test_parallel_claims_and_lease_loss(installation):
    env = installation
    jobs = {submit(env["alice"], state=str(i)) for i in range(6)}
    workers = [SQLWorker(env["app"], Model()) for _ in range(6)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        claimed = list(pool.map(lambda worker: worker.claim(), workers))
    assert {job["id"] for job in claimed} == jobs
    response = {"output_state": "UNKNOWN", "operation_state": "SUCCEEDED", "value": None}
    for i, (worker, job) in enumerate(zip(workers, claimed)):
        assert worker.heartbeat(job["id"])
        assert not workers[(i + 1) % 6].finish(job["id"], response)
        assert worker.finish(job["id"], response)
    job = submit(env["alice"], state="expired")
    worker = SQLWorker(env["app"], Model())
    assert worker.claim()["id"] == job
    with env["owner"].engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE jev.jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=CAST(:id AS uuid)"
            ),
            {"id": job},
        )
    assert worker.claim() is None
    assert not worker.finish(job, response)
    assert result(env["alice"], job)["job_state"] == "FAILED"


def test_revoked_principal_cannot_dispatch(installation):
    env = installation
    job = submit(env["bob"])
    with env["owner"].engine.begin() as connection:
        connection.execute(
            text("DELETE FROM jev.client_roles WHERE login=:login"), {"login": env["roles"][2]}
        )
    assert SQLWorker(env["app"], Model()).claim() is None
    with pytest.raises(DBAPIError):
        result(env["bob"], job)
