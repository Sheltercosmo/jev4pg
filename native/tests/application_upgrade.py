"""Upgrade populated release data, then use it through both execution engines."""

import json
import os
import secrets
import subprocess
import tempfile
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy import URL, text
from sqlalchemy.exc import DBAPIError

from application_upgrade_fixture import Model, job_result, submit
from sdd.bootstrap import SCHEMA_VERSION, migrate
from sdd.db import Database
from sdd.deployment import check_database
from sdd.generic.catalog import Catalog, serial
from sdd.generic.features import FeatureRegistry
from sdd.generic.history import QueryHistory
from sdd.generic.source_catalog import attach
from sdd.generic.sql import SQLService
from sdd.sql_worker import SQLWorker


def fingerprints(connection, names):
    result = {}
    for oid in names:
        namespace, name = connection.execute(
            "SELECT n.nspname,c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.oid=%s",
            (oid,),
        ).fetchone()
        result[oid] = connection.execute(
            sql.SQL(
                "SELECT count(*),md5(coalesce(string_agg(to_jsonb(t)::text,E'\\n' ORDER BY to_jsonb(t)::text),'')) FROM {}.{} t"
            ).format(sql.Identifier(namespace), sql.Identifier(name))
        ).fetchone()
    return result


def verify_application_upgrade(admin, release_python):
    database = "release_upgrade"
    roles = ("release_upgrade_app", "release_upgrade_alice", "release_upgrade_bob")
    password = secrets.token_urlsafe(32)
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    owner = psycopg.connect(admin.info.dsn, dbname=database, autocommit=True)
    url = URL.create(
        "postgresql+psycopg",
        username=owner.info.user,
        host=owner.info.host,
        port=owner.info.port,
        database=database,
    )
    databases = []
    try:
        with tempfile.TemporaryDirectory(prefix="jev-release-upgrade-") as temporary:
            state_path = Path(temporary) / "state.json"
            subprocess.run(
                [
                    str(Path(release_python).absolute()),
                    "-I",
                    str(Path(__file__).with_name("application_upgrade_fixture.py").resolve()),
                    str(state_path),
                ],
                check=True,
                cwd=temporary,
                env={
                    **os.environ,
                    "SDD_UPGRADE_ADMIN_URL": url.render_as_string(hide_password=False),
                    "SDD_UPGRADE_PASSWORD": password,
                    "SDD_SEMANTIC_ENGINE": "python",
                },
                timeout=90,
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))
        tables = [
            row[0]
            for row in owner.execute(
                "SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE c.relkind='r' AND n.nspname IN ('public','sdd_data','jev') "
                "AND c.relname<>'sdd_schema_version' ORDER BY 1"
            ).fetchall()
        ]
        before = fingerprints(owner, tables)
        assert len([value for value in before.values() if value[0]]) >= 12
        passwords = owner.execute(
            "SELECT rolname,rolpassword FROM pg_authid WHERE rolname=ANY(%s) ORDER BY 1",
            (list(roles),),
        ).fetchall()
        try:
            migrate(url, roles[0], sql_interface=True, native_interface=True, native_registry=True)
        except ValueError as error:
            assert "password" in str(error)
        else:
            raise AssertionError("Incomplete native setup was accepted")
        assert owner.execute("SELECT version FROM sdd_schema_version").fetchone()[0] == 1
        assert owner.execute("SELECT to_regnamespace('jev_native') IS NULL").fetchone()[0]
        assert fingerprints(owner, tables) == before

        migrate(
            url,
            roles[0],
            "replacement-password-that-must-not-be-applied",
            sql_interface=True,
            native_interface=True,
            native_registry=True,
            native_registry_password=password,
        )
        migrate(url, roles[0], sql_interface=True, native_interface=True, native_registry=True)
        assert (
            owner.execute("SELECT version FROM sdd_catalog.sdd_schema_version").fetchone()[0]
            == SCHEMA_VERSION
        )
        assert fingerprints(owner, tables) == before
        assert (
            owner.execute(
                "SELECT rolname,rolpassword FROM pg_authid WHERE rolname=ANY(%s) ORDER BY 1",
                (list(roles),),
            ).fetchall()
            == passwords
        )
        assert (
            owner.execute("SELECT count(*) FROM sdd_catalog.dataset_source_bindings").fetchone()[0]
            == 0
        )
        app = Database(url.set(username=roles[0], password=password))
        databases.append(app)
        assert check_database(app, sql_interface=True)["database"] == "ready"
        catalog, history, features = Catalog(app), QueryHistory(app), FeatureRegistry(app)
        model = Model()
        python = SQLService(app, model, semantic_engine="python")
        native = SQLService(app, semantic_engine="native")

        for item in state["tenants"]:
            tenant, actor = item["tenant"], item["actor"]
            dataset = catalog.get(tenant, item["dataset"]["id"])
            assert dataset == item["dataset"]
            assert serial(catalog.rows(tenant, dataset)) == item["rows"]
            assert features.get(tenant, item["feature"])["materialization"]["run_id"]
            saved = history.detail(tenant, actor, item["history"])
            assert saved["output"]["result"] == item["result"]
            assert saved["output"]["executed"] is False
            calls = model.calls
            assert (
                python.execute(tenant, item["query"], max_evaluations=0)["result"] == item["result"]
            )
            assert model.calls == calls
            revised = history.capture(
                tenant,
                actor,
                {
                    "mode": "sql",
                    "text": "SELECT seq FROM records WHERE seq=1",
                    "dataset_ids": [dataset["id"]],
                },
                lambda: native.execute(tenant, "SELECT seq FROM records WHERE seq=1"),
                parent_id=item["history"],
            )
            assert revised["result"] == [{"seq": 1}]
            assert history.get(tenant, actor, revised["history_id"])["parent_id"] == item["history"]
            semantic = native.execute(
                tenant, "SELECT seq,SEMANTIC(\"正文\",'true') AS selected FROM records ORDER BY seq"
            )
            assert semantic["manifest"]["execution_backend"] == "rust_postgresql"
            assert semantic["result"] == [{"seq": index, "selected": True} for index in (1, 2, 3)]
            client = Database(url.set(username=item["login"], password=password))
            databases.append(client)
            for label, job in item["jobs"].items():
                assert job_result(client, job) == item["job_results"][label]
            running = job_result(client, item["running"])
            assert running["job_state"] == "RUNNING" and running["output_state"] == "NOT_EVALUATED"
            with owner.transaction():
                owner.execute(
                    "UPDATE jev.jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
                    (item["running"],),
                )
            worker = SQLWorker(app, model)
            calls = model.calls
            assert worker.claim() is None and model.calls == calls
            assert job_result(client, item["running"])["operation_state"] == "FAILED"
            job = submit(client)
            assert worker.work_one()
            assert job_result(client, job)["output_state"] == "VALUE"
            with client.engine.begin() as connection:
                other = next(record for record in state["tenants"] if record["tenant"] != tenant)
                try:
                    connection.execute(
                        text("SELECT set_config('sdd.tenant',:tenant,true)"),
                        {"tenant": other["tenant"]},
                    )
                    connection.execute(
                        text("SELECT jev.result(CAST(:job AS uuid))"),
                        {"job": other["jobs"]["value"]},
                    )
                except DBAPIError:
                    connection.rollback()
                else:
                    raise AssertionError("Upgrade lost SQL client isolation")
            try:
                history.detail(tenant, "different-actor", item["history"])
            except ValueError:
                pass
            else:
                raise AssertionError("Upgrade lost history ownership")
            committed = python.commit(tenant, item["preview"], actor)
            assert committed["manifest"]["committed"]
            assert python.execute(tenant, "SELECT amount FROM records WHERE seq=3")["result"] == [
                {"amount": "12.2500000000"}
            ]

        owner.execute("CREATE SCHEMA upgraded_source AUTHORIZATION release_upgrade_app")
        with app.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE upgraded_source.notes(id integer PRIMARY KEY, body text)"
            )
            connection.exec_driver_sql(
                "INSERT INTO upgraded_source.notes VALUES (1,'新数据'),(2,NULL)"
            )
        attached = attach(catalog, "upgrade-a", "new_notes", "upgraded_source", "notes")
        assert attached["source_binding"]
        assert native.execute("upgrade-a", "SELECT * FROM new_notes ORDER BY id")["result"] == [
            {"id": 1, "body": "新数据"},
            {"id": 2, "body": None},
        ]
        assert len(catalog.list("upgrade-b")) == 1
    finally:
        for db in databases:
            db.engine.dispose()
        owner.close()
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
        for role in (*roles, "jev_registry"):
            admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
    return [
        "Published v0.6.0 application data survives failed, successful and repeated migration without changed rows or passwords",
        "Upgraded typed data, reviewed features, zero-call evidence and owned query history remain usable",
        "Upgrade preserves SQL output states and isolates tenants; expired workers are fenced without replay",
        "Pre-upgrade mutation previews remain usable and native reads plus source attachments work after migration",
    ]
