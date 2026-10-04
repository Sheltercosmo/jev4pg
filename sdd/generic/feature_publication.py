"""Publish evaluated feature populations against their source and review contract."""

from contextlib import contextmanager

from sqlalchemy import select, update

from ..ledger import digest, now
from . import schema
from .catalog import serial
from .source_catalog import qualified, source_transaction


def _revisions(connection, tenant, feature_ids):
    features = connection.execute(
        select(schema.features).where(
            schema.features.c.tenant == tenant, schema.features.c.id.in_(feature_ids)
        )
    ).mappings()
    return {
        item["id"]: digest(
            {key: item[key] for key in ("definition", "status", "review", "materialization")}
        )
        for item in features
    }


def _assertions(connection, tenant, feature_ids):
    return digest(
        sorted(
            connection.execute(
                select(schema.feature_reviews.c.id).where(
                    schema.feature_reviews.c.tenant == tenant,
                    schema.feature_reviews.c.feature_id.in_(feature_ids),
                )
            ).scalars()
        )
    )


def capture(registry, tenant, dataset, features):
    identities = [feature["id"] for feature in features]
    with registry.db.transaction(tenant) as connection:
        lock_definitions(connection, tenant, dataset["id"])
        revisions = _revisions(connection, tenant, identities)
        expected = {
            feature["id"]: digest(
                {key: feature[key] for key in ("definition", "status", "review", "materialization")}
            )
            for feature in features
        }
        if revisions != expected:
            raise ValueError("Feature definitions changed before refresh; load them again")
        return {
            "version": 1,
            "dataset_id": dataset["id"],
            "revisions": revisions,
            "assertions": _assertions(connection, tenant, identities),
        }


def lock_definitions(connection, tenant, dataset_id):
    connection.execute(
        select(schema.datasets.c.id)
        .where(schema.datasets.c.id == dataset_id, schema.datasets.c.tenant == tenant)
        .with_for_update()
    ).scalar_one()


@contextmanager
def publication_transaction(registry, tenant, dataset):
    with source_transaction(registry.db, tenant, [dataset]) as connection:
        if connection.dialect.name == "postgresql":
            connection.exec_driver_sql("SET LOCAL lock_timeout = '3000ms'")
            connection.exec_driver_sql("SET LOCAL statement_timeout = '10000ms'")
            if not dataset.get("source_binding"):
                relation = qualified(connection, dataset["schema_name"], dataset["table_name"])
                connection.exec_driver_sql(f"LOCK TABLE {relation} IN SHARE MODE")
        else:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        lock_definitions(connection, tenant, dataset["id"])
        yield connection


def blocked(reason):
    return {
        "output_state": "NOT_EVALUATED",
        "operation_state": "BLOCKED_BY_DEPENDENCY",
        "reason": reason,
    }


def publish(registry, connection, tenant, dataset, result):
    contract = result["refresh_contract"]
    identities = list(contract["revisions"])
    manifest = result["manifest"]
    evaluated = {item.get("feature_id") for item in manifest.get("evidence", [])}
    if (
        contract["version"] != 1
        or contract["dataset_id"] != dataset["id"]
        or manifest["dataset_ids"] != [dataset["id"]]
        or manifest["snapshot_mode"] != "content_hash"
        or evaluated != set(identities)
    ):
        return blocked("RefreshContractMismatch")
    if not manifest["complete"]:
        return blocked("UnresolvedFeatureValues")
    run = connection.execute(
        select(schema.runs.c.manifest).where(
            schema.runs.c.tenant == tenant, schema.runs.c.id == result["run_id"]
        )
    ).scalar_one_or_none()
    if run is None or run.get("status") == "redacted_source_deleted":
        return blocked("EvaluatedRunUnavailable")
    if _revisions(connection, tenant, identities) != contract["revisions"]:
        return blocked("FeatureRevisionOrGenerationChanged")
    if _assertions(connection, tenant, identities) != contract["assertions"]:
        return blocked("FeatureAssertionsChanged")
    rows = registry.catalog.rows(tenant, dataset, connection, limit=50001)
    if digest(serial({dataset["id"]: rows})) != manifest["source_snapshot"]:
        return blocked("SourceChangedAfterEvaluation")
    reference = {
        "run_id": result["run_id"],
        "source_snapshot": manifest["source_snapshot"],
        "semantic_snapshot": manifest["semantic_snapshot"],
        "assertion_snapshot": contract["assertions"],
        "refreshed_at": now(),
        "freshness": "checked_at_publication",
    }
    connection.execute(
        update(schema.features)
        .where(schema.features.c.tenant == tenant, schema.features.c.id.in_(identities))
        .values(materialization=reference)
    )
    return {"output_state": "VALUE", "operation_state": "SUCCEEDED", "value": reference}


def record(connection, tenant, result, outcome):
    result["publication"] = outcome
    result["manifest"]["feature_publication"] = outcome
    connection.execute(
        update(schema.runs)
        .where(
            schema.runs.c.tenant == tenant,
            schema.runs.c.id == result["run_id"],
            schema.runs.c.manifest["status"]
            .as_string()
            .is_distinct_from("redacted_source_deleted"),
        )
        .values(manifest=serial(result["manifest"]))
    )
