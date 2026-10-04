"""Bounded application connections, view guards and verified TLS."""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, TimeoutError

from sdd.db import Database
from sdd.api import create_app
from sdd.execution import Executor
from sdd.generic.source_catalog import source_transaction
from test_deployment_postgres import installation as installation
from test_source_catalog_postgres import source as source

pytestmark = pytest.mark.skipif(
    not os.getenv("SDD_TEST_ADMIN_URL"), reason="Dedicated admin test URL required"
)


@pytest.fixture
def bounded(installation, monkeypatch):
    for key, value in {
        "SDD_DB_POOL_SIZE": "1",
        "SDD_DB_MAX_OVERFLOW": "0",
        "SDD_DB_GUARD_POOL_SIZE": "2",
        "SDD_DB_POOL_TIMEOUT": "0.2",
    }.items():
        monkeypatch.setenv(key, value)
    name = "connection-test-" + uuid.uuid4().hex
    db = Database(installation["app"].engine.url.update_query_dict({"application_name": name}))
    yield db, name
    db.engine.dispose()


def test_guard_capacity_does_not_consume_main_slots_and_is_reused(bounded, installation):
    db, name = bounded
    barrier = Barrier(3)

    def hold_guard(tenant):
        with db.source_guard(tenant) as connection:
            identity = connection.exec_driver_sql(
                "SELECT pg_backend_pid(),current_setting('sdd.tenant')"
            ).one()
            barrier.wait(timeout=5)
            barrier.wait(timeout=5)
            return identity

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(hold_guard, tenant) for tenant in ("a", "乙")]
        barrier.wait(timeout=5)
        try:
            with pytest.raises(TimeoutError), db.source_guard("overflow"):
                pytest.fail("Guard pool exceeded its configured capacity")
            with db.transaction("main") as connection:
                assert (
                    connection.exec_driver_sql("SELECT current_setting('sdd.tenant')").scalar_one()
                    == "main"
                )
                with pytest.raises(TimeoutError), db.transaction("overflow"):
                    pytest.fail("Main pool exceeded its configured capacity")
                with installation["owner"].engine.connect() as owner:
                    assert (
                        owner.execute(
                            text(
                                "SELECT count(*) FROM pg_stat_activity WHERE application_name=:name"
                            ),
                            {"name": name},
                        ).scalar_one()
                        == 3
                    )
        finally:
            barrier.wait(timeout=5)
        identities = [future.result(timeout=5) for future in futures]
    assert {row[1] for row in identities} == {"a", "乙"}
    for _ in range(5):
        with db.source_guard("reused") as connection:
            identity, tenant = connection.exec_driver_sql(
                "SELECT pg_backend_pid(),current_setting('sdd.tenant')"
            ).one()
            assert identity in {row[0] for row in identities} and tenant == "reused"
    db.engine.dispose()
    with installation["owner"].engine.connect() as owner:
        assert (
            owner.execute(
                text("SELECT count(*) FROM pg_stat_activity WHERE application_name=:name"),
                {"name": name},
            ).scalar_one()
            == 0
        )


def test_main_checkout_timeout_releases_view_guard_and_locks(source, bounded):
    db, _ = bounded
    name, role = source["schema"], source["roles"][0]
    with source["owner"].engine.begin() as owner:
        owner.exec_driver_sql(
            f'CREATE MATERIALIZED VIEW "{name}".snapshot AS SELECT * FROM "{name}".records'
        )
        owner.exec_driver_sql(f'GRANT SELECT ON "{name}".snapshot TO "{role}"')
    dataset = source["catalog"].attach("tenant-a", "snapshot", name, "snapshot")
    with db.transaction("tenant-a"):
        with pytest.raises(TimeoutError), source_transaction(db, "tenant-a", [dataset]):
            pytest.fail("Main connection should have timed out")
        with source["owner"].engine.begin() as owner:
            owner.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
            owner.exec_driver_sql(f'REFRESH MATERIALIZED VIEW "{name}".snapshot')
    with source_transaction(db, "tenant-a", [dataset]) as connection:
        assert (
            connection.exec_driver_sql(f'SELECT count(*) FROM "{name}".snapshot').scalar_one() == 3
        )


def test_dead_idle_connections_are_replaced_in_both_pools(bounded, installation):
    db, _ = bounded
    for transaction in (db.transaction, db.source_guard):
        with transaction("before") as connection:
            previous = connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
        with installation["owner"].engine.connect() as owner:
            assert owner.execute(
                text("SELECT pg_terminate_backend(:pid, 5000)"), {"pid": previous}
            ).scalar_one()
        with transaction("after") as connection:
            current, tenant = connection.exec_driver_sql(
                "SELECT pg_backend_pid(),current_setting('sdd.tenant')"
            ).one()
            assert current != previous and tenant == "after"


def test_http_capacity_response_recovers_without_connection_details(bounded):
    db, _ = bounded
    app = create_app(
        Executor(db, {}),
        tokens={"capacity-test": {"tenant": "capacity", "name": "reader", "role": "reader"}},
    )
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer capacity-test"}
        with db.transaction("occupied"):
            response = client.get("/datasets", headers=headers)
            assert response.status_code == 503
            assert response.json() == {
                "detail": "Database connection capacity is temporarily exhausted.",
                "code": "database_capacity",
            }
            assert "retry-after" not in response.headers
        assert client.get("/datasets", headers=headers).json() == {"datasets": []}


@pytest.mark.skipif(not os.getenv("SDD_TEST_TLS_ROOTCERT"), reason="TLS test server required")
def test_tls_verifies_main_and_guard_connections_and_rejects_wrong_identity(installation, tmp_path):
    url = installation["app"].engine.url.update_query_dict(
        {
            "sslmode": "verify-full",
            "sslrootcert": os.environ["SDD_TEST_TLS_ROOTCERT"],
            "connect_timeout": "2",
        }
    )
    db = Database(url)
    try:
        for transaction in (db.transaction, db.source_guard):
            with transaction("tls") as connection:
                assert connection.exec_driver_sql(
                    "SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()"
                ).scalar_one()
    finally:
        db.engine.dispose()
    invalid_ca = tmp_path / "invalid-ca.crt"
    invalid_ca.write_text("not a trusted certificate")
    bad_urls = [
        (
            url.set(host="wrong-host.invalid").update_query_dict({"hostaddr": "127.0.0.1"}),
            "does not match host name",
        ),
        (url.update_query_dict({"sslrootcert": str(invalid_ca)}), "root certificate file"),
        (
            url.update_query_dict({"sslrootcert": str(tmp_path / "missing-ca.crt")}),
            "root certificate file",
        ),
    ]
    if os.getenv("SDD_TEST_TLS_UNTRUSTED_CA"):
        bad_urls.append(
            (
                url.update_query_dict({"sslrootcert": os.environ["SDD_TEST_TLS_UNTRUSTED_CA"]}),
                "certificate verify failed",
            )
        )
    for bad, reason in bad_urls:
        db = Database(bad)
        try:
            for transaction in (db.transaction, db.source_guard):
                with pytest.raises(DBAPIError, match=reason), transaction("tls"):
                    pytest.fail("Unverified TLS identity was accepted")
        finally:
            db.engine.dispose()
