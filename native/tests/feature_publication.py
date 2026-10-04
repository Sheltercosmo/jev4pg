"""Exercise feature publication with independent PostgreSQL connections."""

import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest.mock import patch

from psycopg import sql
from sqlalchemy import URL, select, text, update

from application_upgrade_fixture import Model
from sdd.bootstrap import migrate
from sdd.db import Database
from sdd.generic import feature_publication
from sdd.generic.catalog import Catalog
from sdd.generic.features import FeatureRegistry
from sdd.generic.sql import SQLService


def verify_feature_publication(admin):
    name, role = "feature_publication_test", "feature_publication_app"
    password = secrets.token_urlsafe(32)
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = URL.create(
        "postgresql+psycopg",
        username=admin.info.user,
        host=admin.info.host,
        port=admin.info.port,
        database=name,
    )
    db = None
    try:
        migrate(url, role, password)
        db = Database(url.set(username=role, password=password))
        catalog, registry, model = Catalog(db), FeatureRegistry(db), Model()
        for tenant, table_name, subject, key, definition in (
            ("en", "notes", "body", "id", "The author requests follow-up."),
            ("zh", "事项", "正文", "编号", "作者要求继续跟进。"),
        ):
            dataset = catalog.create(
                tenant,
                table_name,
                [
                    {key: i, subject: value, "amount": Decimal("9007199254740993.1234567890")}
                    for i, value in ((1, "Please follow up."), (2, "请继续跟进。"))
                ],
                primary_key=[key, "amount"],
            )
            feature = registry.create(
                tenant,
                "reviewer",
                dataset["id"],
                "action",
                subject,
                definition,
                maintain=True,
            )
            registry.review(
                tenant,
                feature["id"],
                "reviewer",
                "active",
                "Reviewed",
                [{"text": "Please follow up.", "expected": True}],
            )
            first = registry.refresh(tenant, dataset["id"], model)
            assert first["publication"]["output_state"] == "VALUE"
            original = SQLService.execute

            def changed(service, *args, **kwargs):
                result = original(service, *args, **kwargs)
                with db.transaction(tenant) as connection:
                    table = catalog.table(dataset, connection)
                    connection.execute(
                        update(table).where(table.c[key] == 1).values({subject: "Changed"})
                    )
                return result

            with patch.object(SQLService, "execute", changed):
                held = registry.refresh(tenant, dataset["id"], model)
            assert held["publication"]["reason"] == "SourceChangedAfterEvaluation"
            assert (
                registry.get(tenant, feature["id"])["materialization"]["run_id"] == first["run_id"]
            )
            assert (
                registry.refresh(tenant, dataset["id"], model)["publication"]["output_state"]
                == "VALUE"
            )
            before = model.calls
            assert (
                registry.refresh(tenant, dataset["id"], model, max_evaluations=0)["publication"][
                    "output_state"
                ]
                == "VALUE"
            )
            assert model.calls == before

        entered, release, writer_started = (threading.Event() for _ in range(3))
        original_publish = feature_publication.publish
        writer_pid = []

        def waiting(*args):
            entered.set()
            assert release.wait(10)
            return original_publish(*args)

        def write():
            with db.transaction(tenant) as connection:
                writer_pid.append(connection.execute(text("SELECT pg_backend_pid()")).scalar_one())
                table = catalog.table(dataset, connection)
                writer_started.set()
                connection.execute(
                    update(table).where(table.c[key] == 2).values({subject: "After publication"})
                )

        with patch.object(feature_publication, "publish", waiting), ThreadPoolExecutor(2) as pool:
            published = pool.submit(registry.refresh, tenant, dataset["id"], model)
            try:
                assert entered.wait(10)
                writer = pool.submit(write)
                assert writer_started.wait(5)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    blocked = admin.execute(
                        "SELECT cardinality(pg_blocking_pids(%s))", (writer_pid[0],)
                    ).fetchone()[0]
                    if blocked:
                        break
                    time.sleep(0.02)
                assert blocked and not writer.done()
                with db.transaction(tenant) as connection:
                    table = catalog.table(dataset, connection)
                    assert len(connection.execute(select(table)).all()) == 2
            finally:
                release.set()
            assert published.result(timeout=10)["publication"]["output_state"] == "VALUE"
            writer.result(timeout=10)

        before = registry.get(tenant, feature["id"])["materialization"]
        with patch.object(
            feature_publication, "record", side_effect=RuntimeError("Commit interrupted")
        ):
            try:
                registry.refresh(tenant, dataset["id"], model)
            except RuntimeError:
                pass
            else:
                raise AssertionError("Interrupted publication committed")
        assert registry.get(tenant, feature["id"])["materialization"] == before
    finally:
        if db:
            db.engine.dispose()
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
    return [
        "Feature publication rechecks exact source populations across English and renamed Chinese schemas with composite decimal keys",
        "Stable feature refresh publishes with zero new-call allowance and preserves evidence reuse",
        "Imported-source writes wait only for the publication transaction while ordinary readers continue",
        "Interrupted feature publication rolls back its materialization reference atomically",
    ]
