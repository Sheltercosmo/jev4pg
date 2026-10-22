import os
from pathlib import Path
import secrets
import subprocess
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from sdd.sql_install import install, installed, portable_script


def test_portable_uses_the_same_queue_without_extension_registration():
    sql = portable_script()
    assert "pg_extension_config_dump" not in sql
    assert "SECURITY DEFINER SET search_path = pg_catalog" in sql
    assert "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA jev FROM PUBLIC" in sql


@pytest.fixture
def database():
    if not os.getenv("SDD_TEST_ADMIN_URL"):
        pytest.skip("Dedicated admin test URL required")
    base = make_url(os.environ["SDD_TEST_ADMIN_URL"])
    name = "portable_" + uuid.uuid4().hex[:12]
    password = secrets.token_urlsafe(32)
    admin = create_engine(base, isolation_level="AUTOCOMMIT", hide_parameters=True)
    with admin.connect() as cx:
        cx.exec_driver_sql(f"CREATE ROLE \"{name}\" LOGIN PASSWORD '{password}'")
        cx.exec_driver_sql(f'CREATE DATABASE "{name}" OWNER "{name}"')
    owner = create_engine(
        base.set(database=name, username=name, password=password), hide_parameters=True
    )
    try:
        yield owner
    finally:
        owner.dispose()
        with admin.connect() as cx:
            cx.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
            cx.exec_driver_sql(f'DROP ROLE "{name}"')
        admin.dispose()


def test_non_superuser_install_preflight_repeat_and_states(database):
    with database.begin() as cx:
        assert (
            cx.execute(text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")).scalar()
            is False
        )
        assert install(cx, "portable", check=True)["action"] == "create"
        assert cx.execute(text("SELECT to_regnamespace('jev')")).scalar() is None
        install(cx, "portable")
        assert install(cx, "portable")["action"] == "preserve"
        assert installed(cx) == {"mode": "portable", "version": "0.1.0"}
        assert (
            cx.execute(text("SELECT count(*) FROM pg_extension WHERE extname='jevsd_pg'")).scalar()
            == 0
        )
        cx.execute(text("INSERT INTO jev.client_roles VALUES (current_user,'a','owner','reader')"))
        job = cx.execute(
            text('SELECT jev.submit(\'NOUL\', \'{"state":"你好","proposition":"需要跟进？"}\')')
        ).scalar_one()
        response = cx.execute(text("SELECT jev.result(:job)"), {"job": job}).scalar_one()
        assert response["output_state"] == "NOT_EVALUATED"
        assert response["value"] is None
        assert install(cx, "portable")["action"] == "preserve"
        assert cx.execute(text("SELECT count(*) FROM jev.jobs")).scalar_one() == 1
    with database.begin() as cx, pytest.raises(ValueError, match="existing SQL installation mode"):
        install(cx, "extension")


def test_unmanaged_schema_is_not_adopted(database):
    with database.begin() as cx:
        cx.exec_driver_sql("CREATE SCHEMA jev; CREATE TABLE jev.business_data (id int)")
    with database.begin() as cx, pytest.raises(ValueError, match="unmanaged"):
        install(cx, "portable")
    with database.connect() as cx:
        assert cx.execute(text("SELECT to_regclass('jev.business_data')")).scalar() is not None


def test_installation_rollback_is_atomic(database):
    with pytest.raises(RuntimeError), database.begin() as cx:
        install(cx, "portable")
        raise RuntimeError("migration interrupted")
    with database.connect() as cx:
        assert cx.execute(text("SELECT to_regnamespace('jev')")).scalar() is None


def test_changed_marker_blocks_repeat_install(database):
    with database.begin() as cx:
        install(cx, "portable")
        cx.execute(text("UPDATE jev.installation SET script_sha256='changed'"))
    with database.begin() as cx, pytest.raises(ValueError, match="does not match"):
        install(cx, "portable")


def test_marker_view_is_not_read(database):
    with database.begin() as cx:
        cx.exec_driver_sql(
            "CREATE SCHEMA jev; CREATE VIEW jev.installation AS SELECT '0.1.0' AS version"
        )
    with database.begin() as cx, pytest.raises(ValueError, match="unexpected kind"):
        install(cx, "portable")


def test_portable_dump_restore_keeps_jobs_and_functions(database, tmp_path):
    if not os.getenv("SDD_TEST_PG_BIN"):
        pytest.skip("PostgreSQL client binary directory required")
    pg = Path(os.environ["SDD_TEST_PG_BIN"])
    suffix = ".exe" if os.name == "nt" else ""
    windows = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    with database.begin() as cx:
        install(cx, "portable")
        cx.exec_driver_sql(
            "INSERT INTO jev.client_roles VALUES (current_user,'a','owner','reader')"
        )
        job = cx.execute(text("SELECT jev.submit('NOUL','{}')")).scalar_one()
    archive = tmp_path / "portable.dump"
    url = database.url
    environment = {
        **os.environ,
        "PGHOST": url.host,
        "PGPORT": str(url.port),
        "PGUSER": url.username,
        "PGPASSWORD": url.password,
        "PGDATABASE": url.database,
    }
    subprocess.run(
        [str(pg / ("pg_dump" + suffix)), "-Fc", "-f", str(archive)],
        env=environment,
        check=True,
        capture_output=True,
        **windows,
    )
    destination = "restore_" + uuid.uuid4().hex[:12]
    admin = create_engine(os.environ["SDD_TEST_ADMIN_URL"], isolation_level="AUTOCOMMIT")
    restored = create_engine(url.set(database=destination))
    try:
        with admin.connect() as cx:
            cx.exec_driver_sql(f'CREATE DATABASE "{destination}" OWNER "{url.username}"')
        subprocess.run(
            [
                str(pg / ("pg_restore" + suffix)),
                "--exit-on-error",
                "--no-owner",
                "-d",
                destination,
                str(archive),
            ],
            env=environment,
            check=True,
            capture_output=True,
            **windows,
        )
        with restored.begin() as cx:
            assert install(cx, "portable")["action"] == "preserve"
            assert (
                cx.execute(text("SELECT jev.result(:job)"), {"job": job}).scalar_one()["job_state"]
                == "QUEUED"
            )
    finally:
        restored.dispose()
        with admin.connect() as cx:
            cx.exec_driver_sql(f'DROP DATABASE IF EXISTS "{destination}" WITH (FORCE)')
        admin.dispose()
