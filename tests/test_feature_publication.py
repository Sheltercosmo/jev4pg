import pytest
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import SQLAlchemyError

from sdd.generic import schema
from sdd.generic.sql import SQLService
from test_semantic_upgrade import feature, system as system


def after_evaluation(monkeypatch, action):
    original = SQLService.execute
    armed = True

    def execute(*args, **kwargs):
        nonlocal armed
        result = original(*args, **kwargs)
        if armed:
            armed = False
            action(result)
        return result

    monkeypatch.setattr(SQLService, "execute", execute)


@pytest.mark.parametrize("change", ["update", "insert", "delete", "review", "deprecate"])
def test_refresh_rejects_changes_between_evaluation_and_publication(system, monkeypatch, change):
    db, catalog, dataset, model, _, registry = system
    active = feature(system, maintain=True)
    first = registry.refresh("a", dataset["id"], model)
    assert first["publication"]["output_state"] == "VALUE"
    before = registry.get("a", active["id"])["materialization"]

    def modify(_):
        if change == "review":
            registry.assert_value("a", active["id"], {"id": 1}, False, "r", "Changed correction")
        elif change == "deprecate":
            registry.review("a", active["id"], "r", "deprecated", "Retired definition")
        else:
            with db.transaction("a") as connection:
                table = catalog.table(dataset, connection)
                statements = {
                    "update": update(table).where(table.c.id == 0).values(body="已经更改"),
                    "insert": insert(table).values(id=100, body="New record", amount=1),
                    "delete": delete(table).where(table.c.id == 0),
                }
                connection.execute(statements[change])

    after_evaluation(monkeypatch, modify)
    result = registry.refresh("a", dataset["id"], model)
    assert result["manifest"]["complete"]
    assert result["publication"]["output_state"] == "NOT_EVALUATED"
    assert result["publication"]["operation_state"] == "BLOCKED_BY_DEPENDENCY"
    assert registry.get("a", active["id"])["materialization"] == before
    saved = registry.catalog.ledger.get("a", schema.runs, result["run_id"])
    assert saved["manifest"]["feature_publication"] == result["publication"]


def test_older_refresh_cannot_replace_a_newer_generation(system, monkeypatch):
    _, _, dataset, model, _, registry = system
    active = feature(system)
    newer = []
    after_evaluation(
        monkeypatch, lambda _: newer.append(registry.refresh("a", dataset["id"], model))
    )
    older = registry.refresh("a", dataset["id"], model)
    assert newer[0]["publication"]["output_state"] == "VALUE"
    assert older["publication"]["reason"] == "FeatureRevisionOrGenerationChanged"
    assert registry.get("a", active["id"])["materialization"]["run_id"] == newer[0]["run_id"]
    assert len(model.calls) == 8


@pytest.mark.parametrize("lost", ["expired", "replaced", "cancelled"])
def test_worker_cannot_publish_after_losing_its_lease(system, monkeypatch, lost):
    db, _, _, model, _, registry = system
    active = feature(system, maintain=True)
    changes = {
        "expired": {"lease_until": 0},
        "replaced": {"lease_token": "replacement-worker"},
        "cancelled": {"state": "cancelled", "lease_token": None},
    }

    def lose_lease(_):
        with db.transaction("a") as connection:
            connection.execute(update(schema.maintenance_jobs).values(**changes[lost]))

    after_evaluation(monkeypatch, lose_lease)
    assert registry.work_one("a", model)
    assert registry.get("a", active["id"])["materialization"] == {}
    with db.transaction("a") as connection:
        assert (
            connection.execute(select(schema.maintenance_jobs.c.state)).scalar_one() != "succeeded"
        )
    run = registry.catalog.ledger.list("a", schema.runs)[-1]
    assert run["manifest"]["feature_publication"]["reason"] == "RefreshLeaseLost"


def test_worker_retries_stale_publication_without_repeating_compatible_inference(
    system, monkeypatch
):
    db, _, _, model, _, registry = system
    active = feature(system, maintain=True)
    after_evaluation(
        monkeypatch,
        lambda _: registry.assert_value("a", active["id"], {"id": 1}, False, "r", "Correction"),
    )
    assert registry.work_one("a", model)
    assert registry.get("a", active["id"])["materialization"] == {}
    with db.transaction("a") as connection:
        jobs = connection.execute(select(schema.maintenance_jobs)).mappings().all()
        assert any(job["error"] == "FeatureAssertionsChanged" for job in jobs)
        connection.execute(update(schema.maintenance_jobs).values(available_at=0))
    assert registry.work_one("a", model)
    assert registry.get("a", active["id"])["materialization"]["run_id"]
    assert len(model.calls) == 8


def test_budget_hold_never_publishes_a_false_feature_generation(system):
    _, _, dataset, model, _, registry = system
    active = feature(system)
    result = registry.refresh("a", dataset["id"], model, max_evaluations=0)
    assert not result["manifest"]["complete"]
    assert result["manifest"]["semantic_coverage"]["not_evaluated"] == 8
    assert result["publication"]["reason"] == "UnresolvedFeatureValues"
    assert registry.get("a", active["id"])["materialization"] == {}
    assert not model.calls


def test_correction_enqueues_maintenance_after_a_successful_refresh(system):
    db, _, _, model, _, registry = system
    active = feature(system, maintain=True)
    assert registry.work_one("a", model)
    registry.assert_value("a", active["id"], {"id": 1}, False, "r", "Checked text")
    with db.transaction("a") as connection:
        assert sorted(connection.execute(select(schema.maintenance_jobs.c.state)).scalars()) == [
            "pending",
            "succeeded",
        ]


def test_publication_does_not_restore_a_run_redacted_after_source_deletion(system, monkeypatch):
    _, _, dataset, model, service, registry = system
    active = feature(system)

    def remove(_):
        preview = service.execute("a", "DELETE FROM records WHERE id=0", actor="reviewer")
        service.commit("a", preview["preview_token"], "reviewer")

    after_evaluation(monkeypatch, remove)
    result = registry.refresh("a", dataset["id"], model)
    assert result["publication"]["reason"] == "EvaluatedRunUnavailable"
    run = registry.catalog.ledger.get("a", schema.runs, result["run_id"])
    assert run["manifest"] == {"status": "redacted_source_deleted"}
    assert run["result"] == []
    assert registry.get("a", active["id"])["materialization"] == {}


def test_unknown_and_empty_populations_have_different_publication_outcomes(system):
    db, catalog, dataset, model, _, registry = system
    active = feature(system)
    registry.assert_value("a", active["id"], {"id": 0}, None, "r", "Cannot determine")
    uncertain = registry.refresh("a", dataset["id"], model)
    assert uncertain["publication"]["reason"] == "UnresolvedFeatureValues"
    assert uncertain["manifest"]["semantic_coverage"]["unknown"] == 1
    with db.transaction("a") as connection:
        connection.execute(delete(catalog.table(dataset, connection)))
    calls = len(model.calls)
    empty = registry.refresh("a", dataset["id"], model)
    assert empty["publication"]["output_state"] == "VALUE"
    assert empty["manifest"]["source_rows"] == 0
    assert len(model.calls) == calls


def test_database_failure_without_an_error_code_remains_a_retryable_job(system, monkeypatch):
    db, _, _, model, _, registry = system
    active = feature(system, maintain=True)

    def fail(*args, **kwargs):
        raise SQLAlchemyError("Synthetic database failure")

    monkeypatch.setattr(registry, "refresh", fail)
    assert registry.work_one("a", model)
    with db.transaction("a") as connection:
        job = connection.execute(select(schema.maintenance_jobs)).mappings().one()
    assert job["state"] == "pending" and job["error"] == "SQLAlchemyError"
    assert registry.get("a", active["id"])["materialization"] == {}


def test_lease_loss_before_final_job_update_rolls_back_publication(system, monkeypatch):
    from sdd.generic import feature_publication

    db, _, _, model, _, registry = system
    active = feature(system, maintain=True)
    original = feature_publication.publish

    def expire(registry, connection, tenant, dataset, result):
        outcome = original(registry, connection, tenant, dataset, result)
        connection.execute(update(schema.maintenance_jobs).values(lease_until=0))
        return outcome

    monkeypatch.setattr(feature_publication, "publish", expire)
    with pytest.raises(ValueError, match="lease expired"):
        registry.work_one("a", model)
    assert registry.get("a", active["id"])["materialization"] == {}
    with db.transaction("a") as connection:
        assert connection.execute(select(schema.maintenance_jobs.c.state)).scalar_one() == "running"
