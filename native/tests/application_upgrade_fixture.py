"""Create upgrade state through the installed v0.6.0 application, without model I/O."""

import json
import os
import sys
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url

from sdd.bootstrap import ensure_login, grant_client, migrate
from sdd.db import Database
from sdd.generic.catalog import Catalog, serial
from sdd.generic.features import FeatureRegistry
from sdd.generic.history import QueryHistory
from sdd.generic.sql import SQLService
from sdd.sql_worker import SQLWorker


class Model:
    model = "application-upgrade-fixture"
    identity = "application-upgrade-fixture-v1"

    def __init__(self):
        self.calls = 0

    def ask(self, tenant, state, questions):
        self.calls += 1
        answers = {}
        for key, question in questions.items():
            assert question["type"] == "noul"
            instructions = question["instructions"]
            definition = (
                instructions.get("definition") if isinstance(instructions, dict) else instructions
            )
            answers[key] = {
                "type": "noul",
                "noul": {"unknown": 0.5, "false": 0.05}.get(definition, 0.95),
            }
        return {"model": self.model, "answers": answers, "usage": {"input_tokens": 10}}


def submit(db, proposition="true", **limits):
    with db.engine.begin() as connection:
        return str(
            connection.execute(
                text("SELECT jev.submit('NOUL',CAST(:args AS jsonb),CAST(:limits AS jsonb))"),
                {
                    "args": json.dumps({"state": "原始记录", "proposition": proposition}),
                    "limits": json.dumps(limits),
                },
            ).scalar_one()
        )


def job_result(db, job):
    with db.engine.begin() as connection:
        return connection.execute(
            text("SELECT jev.result(CAST(:job AS uuid))"), {"job": job}
        ).scalar_one()


def seed(path):
    import sdd

    assert version("jevsd-pg") == "0.6.0"
    assert Path(sdd.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    admin_url = make_url(os.environ["SDD_UPGRADE_ADMIN_URL"])
    password = os.environ["SDD_UPGRADE_PASSWORD"]
    migrate(admin_url, "release_upgrade_app", password, sql_interface=True)
    owner = Database(admin_url)
    with owner.engine.begin() as connection:
        assert connection.execute(text("SELECT version FROM sdd_schema_version")).scalar_one() == 1
        for login in ("release_upgrade_alice", "release_upgrade_bob"):
            ensure_login(connection, login, password)
    owner.engine.dispose()
    db = Database(admin_url.set(username="release_upgrade_app", password=password))
    model = Model()
    catalog, features, history = Catalog(db), FeatureRegistry(db), QueryHistory(db)
    sql = SQLService(db, model)
    state = {"version": version("jevsd-pg"), "tenants": []}
    try:
        for tenant, actor in (("upgrade-a", "alice"), ("upgrade-b", "bob")):
            login = "release_upgrade_" + actor
            grant_client(admin_url, login, tenant, actor, "reviewer")
            client = Database(admin_url.set(username=login, password=password))
            try:
                rows = [
                    {
                        "account": tenant,
                        "seq": i,
                        "正文": note,
                        "amount": Decimal(amount),
                        "date": "2026-01-02",
                        "details": {"source": "导入", "missing": None},
                    }
                    for i, note, amount in (
                        (1, "请继续跟进。", "9007199254740993.1234567890"),
                        (2, "O'Reilly says yes", "0.0000000001"),
                        (3, "Please follow up.", "-2.5000000000"),
                    )
                ]
                dataset = catalog.create(tenant, "records", rows, primary_key=["account", "seq"])
                feature = features.create(
                    tenant, actor, dataset["id"], "follow_up", "正文", "true", maintain=True
                )
                features.review(
                    tenant,
                    feature["id"],
                    actor,
                    "active",
                    "Reviewed example",
                    [{"text": "Please follow up.", "expected": True}],
                )
                features.assert_value(
                    tenant,
                    feature["id"],
                    {"account": tenant, "seq": 2},
                    False,
                    actor,
                    "User correction",
                )
                refreshed = features.refresh(tenant, dataset["id"], model)
                assert refreshed["manifest"]["complete"]
                query = "SELECT seq, SEMANTIC_FEATURE(\"正文\",'follow_up') AS follow_up FROM records ORDER BY seq"
                result = history.capture(
                    tenant,
                    actor,
                    {"mode": "sql", "text": query, "dataset_ids": [dataset["id"]]},
                    lambda: sql.execute(
                        tenant, query, request="哪些记录需要继续跟进？", actor=actor
                    ),
                )
                assert result["result"] == [
                    {"seq": 1, "follow_up": True},
                    {"seq": 2, "follow_up": False},
                    {"seq": 3, "follow_up": True},
                ]
                preview = sql.execute(
                    tenant, "UPDATE records SET amount=12.25 WHERE seq=3", actor=actor
                )
                jobs = {}
                for label, proposition, limits in (
                    ("value", "false", {}),
                    ("unknown", "unknown", {}),
                    ("budget", "true", {"max_judgments": 0}),
                ):
                    jobs[label] = submit(client, proposition, **limits)
                    assert SQLWorker(db, model).work_one()
                states = {key: job_result(client, value) for key, value in jobs.items()}
                assert [states[key]["output_state"] for key in ("value", "unknown", "budget")] == [
                    "VALUE",
                    "UNKNOWN",
                    "NOT_EVALUATED",
                ]
                pending = submit(client)
                worker = SQLWorker(db, model)
                claimed = worker.claim()
                assert claimed["id"] == pending
                state["tenants"].append(
                    {
                        "tenant": tenant,
                        "actor": actor,
                        "login": login,
                        "dataset": dataset,
                        "rows": serial(catalog.rows(tenant, dataset)),
                        "feature": feature["id"],
                        "query": query,
                        "result": result["result"],
                        "history": result["history_id"],
                        "preview": preview["preview_token"],
                        "jobs": jobs,
                        "job_results": states,
                        "running": pending,
                        "worker_id": worker.worker_id,
                    }
                )
            finally:
                client.engine.dispose()
        Path(path).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        db.engine.dispose()


if __name__ == "__main__":
    seed(sys.argv[1])
