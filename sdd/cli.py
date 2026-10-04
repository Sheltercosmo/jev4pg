import argparse
import json
import os
import time
from pathlib import Path
from .config import runtime
from . import __version__


def main():
    parser = argparse.ArgumentParser(description="jev4pg database workspace")
    parser.add_argument("--version", action="version", version=f"jev4pg {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("demo")
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--host", default="127.0.0.1")
    migration = sub.add_parser(
        "migrate", help="Install schema and tenant policies as an administrator"
    )
    migration.add_argument("--sql-interface", action="store_true")
    migration.add_argument(
        "--check",
        action="store_true",
        help="Check installation ownership without changing the database",
    )
    migration.add_argument(
        "--native-interface",
        action="store_true",
        help="Enable the installed Rust semantic extension",
    )
    migration.add_argument(
        "--native-registry", action="store_true", help="Create the restricted native evidence login"
    )
    grant = sub.add_parser("sql-grant", help="Map an existing SQL login to a JEV identity")
    grant.add_argument("login")
    grant.add_argument("--tenant", required=True)
    grant.add_argument("--actor", required=True)
    grant.add_argument("--role", choices=("reader", "reviewer"), default="reader")
    extension = sub.add_parser(
        "extension-files", help="Copy SQL extension files for server installation"
    )
    extension.add_argument("destination")
    sql_worker = sub.add_parser("sql-worker", help="Execute durable PostgreSQL operator jobs")
    sql_worker.add_argument("--concurrency", type=int, default=2)
    sql_worker.add_argument("--once", action="store_true")
    sql_worker.add_argument("--heartbeat-file", default="/tmp/jev-sql-worker.heartbeat")
    query_worker = sub.add_parser("query-worker", help="Execute durable application queries")
    query_worker.add_argument("--tenant", action="append", required=True)
    query_worker.add_argument("--concurrency", type=int, default=2)
    query_worker.add_argument("--once", action="store_true")
    query_worker.add_argument("--heartbeat-file")
    ready = sub.add_parser("ready", help="Check database configuration without calling a model")
    ready.add_argument("--worker-heartbeat")
    attachment = sub.add_parser(
        "attach", help="Register an existing PostgreSQL relation without copying data"
    )
    attachment.add_argument("name", help="Logical dataset name")
    attachment.add_argument("--tenant", required=True)
    attachment.add_argument("--schema", required=True)
    attachment.add_argument("--table", required=True)
    attachment.add_argument(
        "--column", action="append", help="Expose only these columns; repeat as needed"
    )
    attachment.add_argument("--description")
    detachment = sub.add_parser(
        "detach", help="Remove an attachment from the catalog; preserve source data"
    )
    detachment.add_argument("dataset")
    detachment.add_argument("--tenant", required=True)
    sources = sub.add_parser("sources", help="Preview or apply a reviewed source manifest")
    source_actions = sources.add_subparsers(dest="source_action", required=True)
    source_preview = source_actions.add_parser(
        "preview", help="Inspect source metadata without writes"
    )
    source_preview.add_argument("file", help="Source manifest JSON file")
    source_preview.add_argument("--tenant", required=True)
    source_preview.add_argument("--output", required=True, help="New review plan JSON file")
    source_apply = source_actions.add_parser("apply", help="Apply a reviewed plan atomically")
    source_apply.add_argument("file", help="Review plan JSON file")
    source_apply.add_argument("--tenant", required=True)
    worker = sub.add_parser("worker")
    worker.add_argument("--tenant", required=True)
    worker.add_argument("--once", action="store_true")
    feature_worker = sub.add_parser("feature-worker", help="Refresh maintained generic features")
    feature_worker.add_argument("--tenant", required=True)
    feature_worker.add_argument("--once", action="store_true")
    maintenance = sub.add_parser("maintain")
    maintenance.add_argument("--tenant", required=True)
    maintenance.add_argument("--once", action="store_true")
    query = sub.add_parser("query")
    query.add_argument("plan")
    query.add_argument("--tenant", required=True)
    natural = sub.add_parser("ask", help="Ask a natural-language question")
    natural.add_argument("question")
    natural.add_argument("--tenant")
    natural.add_argument("--preview", action="store_true")
    natural.add_argument("--max-evaluations", type=int, default=100)
    natural.add_argument(
        "--dataset", action="append", help="Dataset name or ID; repeat to select multiple"
    )
    live = sub.add_parser("live-test")
    live.add_argument("--model", required=True)
    live.add_argument("--output", default="artifacts/live-test.json")
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn

        uvicorn.run("sdd.api:create_app", factory=True, host=args.host, port=args.port)
        return
    if args.command in {
        "migrate",
        "sql-grant",
        "extension-files",
        "ready",
        "attach",
        "detach",
        "sources",
    }:
        from .config import load_env, database_url, secret
        from .bootstrap import migrate, check_migration, grant_client, extension_files
        from .migration_preflight import InstallationConflict

        load_env()
        try:
            if args.command == "migrate" and args.check:
                result = check_migration(
                    database_url(admin=True), os.getenv("SDD_DB_USER", "sdd_app")
                )
            elif args.command == "migrate":
                result = migrate(
                    database_url(admin=True),
                    os.getenv("SDD_DB_USER", "sdd_app"),
                    secret("SDD_DB_PASSWORD"),
                    args.sql_interface,
                    native_interface=args.native_interface,
                    native_registry=args.native_registry,
                    native_registry_password=secret("SDD_NATIVE_REGISTRY_PASSWORD"),
                )
            elif args.command == "sql-grant":
                grant_client(
                    database_url(admin=True), args.login, args.tenant, args.actor, args.role
                )
                result = {"login": args.login, "tenant": args.tenant, "role": args.role}
            elif args.command == "extension-files":
                destination = Path(args.destination)
                destination.mkdir(parents=True, exist_ok=True)
                for source in extension_files().iterdir():
                    if source.name.endswith((".control", ".sql")):
                        (destination / source.name).write_bytes(source.read_bytes())
                result = {"extension_files": str(destination)}
            else:
                from .db import Database
                from .deployment import check_database

                db = Database(database_url())
                try:
                    if args.command == "sources":
                        from .generic.catalog import Catalog
                        from .generic.source_manifest import SourceOnboarding

                        with Path(args.file).open("rb") as input_file:
                            raw = input_file.read(16 * 1024 * 1024 + 1)
                        if len(raw) > 16 * 1024 * 1024:
                            raise ValueError(
                                "Source manifests and review plans must fit within 16 MiB"
                            )
                        document = json.loads(raw)
                        onboarding = SourceOnboarding(Catalog(db))
                        if args.source_action == "preview":
                            result = onboarding.preview(args.tenant, document)
                            with Path(args.output).open("x", encoding="utf-8") as output:
                                json.dump(result, output, ensure_ascii=False, indent=2)
                                output.write("\n")
                            result = {
                                "plan": args.output,
                                "status": "blocked"
                                if any(entry["action"] == "conflict" for entry in result["sources"])
                                else "ready",
                                "fingerprint": result["fingerprint"],
                                "actions": [
                                    {
                                        "name": entry["source"]["name"],
                                        "action": entry["action"],
                                        "reason": entry["reason"],
                                    }
                                    for entry in result["sources"]
                                ],
                            }
                        else:
                            result = onboarding.apply(args.tenant, document)
                    elif args.command in {"attach", "detach"}:
                        from .generic.catalog import Catalog

                        catalog = Catalog(db)
                        result = (
                            catalog.attach(
                                args.tenant,
                                args.name,
                                args.schema,
                                args.table,
                                args.column,
                                args.description,
                            )
                            if args.command == "attach"
                            else catalog.detach(args.tenant, args.dataset)
                        )
                    else:
                        result = check_database(db)
                    if (
                        getattr(args, "worker_heartbeat", None)
                        and time.time() - Path(args.worker_heartbeat).stat().st_mtime > 90
                    ):
                        raise ValueError("SQL worker heartbeat has expired")
                finally:
                    db.engine.dispose()
        except Exception as exc:
            if isinstance(exc, InstallationConflict):
                print(
                    json.dumps(
                        {"status": "blocked", "conflicts": exc.conflicts}, ensure_ascii=False
                    )
                )
                parser.exit(1)
            if args.command in {"attach", "detach", "sources"} and isinstance(exc, ValueError):
                parser.exit(1, f"{args.command} failed: {exc}\n")
            parser.exit(
                1,
                f"{args.command} failed ({type(exc).__name__}). Check configuration, database permissions and server logs.\n",
            )
        print(json.dumps(result))
        if args.command == "sources" and result.get("status") == "blocked":
            parser.exit(1)
        return
    db, executor = runtime()
    if args.command == "sql-worker":
        from .deployment import check_database
        from .generic.api import services
        from .sql_worker import run

        check_database(db, sql_interface=True)
        _, service, _ = services(executor)
        try:
            run(
                db,
                service.decisions,
                concurrency=args.concurrency,
                once=args.once,
                heartbeat_path=args.heartbeat_file,
            )
        finally:
            db.engine.dispose()
    elif args.command == "query-worker":
        from .deployment import check_database
        from .generic.api import services
        from .query_worker import run

        check_database(db)
        _, service, _ = services(executor)
        try:
            run(
                db,
                service.decisions,
                args.tenant,
                concurrency=args.concurrency,
                once=args.once,
                heartbeat_path=args.heartbeat_file,
            )
        finally:
            db.engine.dispose()
    elif args.command == "init":
        db.initialize()
        print("Database initialized.")
    elif args.command == "demo":
        from .demo import demo

        db.initialize()
        print(json.dumps(demo(db), indent=2))
    elif args.command == "worker":
        while True:
            worked = executor.workers.work_one(args.tenant)
            if args.once:
                break
            if not worked:
                time.sleep(1)
    elif args.command == "feature-worker":
        from .generic.api import services
        from .generic.features import FeatureRegistry

        _, service, _ = services(executor)
        if service.decisions is None:
            parser.error("Configure a JEV provider to maintain semantic features")
        registry = FeatureRegistry(db)
        while True:
            worked = registry.work_one(args.tenant, service.decisions)
            if args.once:
                break
            if not worked:
                time.sleep(1)
    elif args.command == "maintain":
        from datetime import datetime, timezone
        from . import schema as s
        from .maintenance import refresh

        while True:
            for m in executor.ledger.list(args.tenant, s.materializations):
                c = executor.ledger.get(args.tenant, s.concepts, m["concept_id"])
                if c["status"] != "active":
                    continue
                if m["last_run"]:
                    last = executor.ledger.get(args.tenant, s.runs, m["last_run"])
                    age = (
                        datetime.now(timezone.utc) - datetime.fromisoformat(last["created_at"])
                    ).total_seconds()
                    if age < m["freshness_seconds"]:
                        continue
                refresh(executor, args.tenant, m["id"])
            if args.once:
                break
            time.sleep(30)
    elif args.command == "query":
        print(
            json.dumps(
                executor.execute(args.tenant, json.loads(Path(args.plan).read_text())), indent=2
            )
        )
    elif args.command == "ask":
        from .generic.api import services

        principals = list(json.loads(os.getenv("SDD_API_TOKENS", "{}")).values())
        tenants = {p["tenant"] for p in principals}
        tenant = args.tenant or (next(iter(tenants)) if len(tenants) == 1 else None)
        if not tenant:
            parser.error(
                "Specify --tenant when local credentials identify zero or multiple tenants."
            )
        owner = next((p["name"] for p in principals if p["tenant"] == tenant), "local-cli")
        _, service, planner = services(executor)
        if planner is None:
            parser.error("Configure a JEV provider to enable planning.")
        from .generic.planning_review import PlanReviews, PlanReviewRequired
        from .generic.history import QueryHistory

        def execute_question():
            plan = PlanReviews(db, planner).begin(tenant, owner, args.question, args.dataset)
            if args.preview:
                return {"plan": plan, "executed": False}
            try:
                return service.execute(
                    tenant,
                    plan["logical_sql"],
                    plan=plan,
                    request=args.question,
                    actor=owner,
                    max_evaluations=args.max_evaluations,
                )
            except Exception as exc:
                exc.query_plan = plan
                raise

        try:
            result = QueryHistory(db).capture(
                tenant,
                owner,
                {
                    "mode": "natural",
                    "text": args.question,
                    "dataset_ids": args.dataset,
                    "max_evaluations": args.max_evaluations,
                },
                execute_question,
            )
        except PlanReviewRequired as exc:
            result = {
                "review_required": True,
                "executed": False,
                "detail": str(exc),
                "plan": exc.plan,
                "history_id": exc.history_id,
            }
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.command == "live-test":
        from .demo import seed

        db.initialize()
        _, plan = seed(db, tenant="live-test", provider="jev", model=args.model)
        cold = executor.execute("live-test", plan)
        warm = executor.execute("live-test", plan)
        output = {"cold": cold, "warm": warm, "same_result": cold["result"] == warm["result"]}
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(output, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "report": str(path),
                    "same_result": output["same_result"],
                    "cold_coverage": cold["manifest"],
                    "warm_coverage": warm["manifest"],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
