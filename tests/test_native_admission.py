import pytest
from sqlalchemy import select

from sdd import schema as base
from sdd.db import Database
from sdd.generic.native_admission import NativeAdmission
from sdd.generic.schema import native_admissions


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("SDD_DAILY_EVALUATIONS", "3")
    database = Database("sqlite:///:memory:")
    database.initialize()
    yield database
    database.engine.dispose()


def used(db, tenant="tenant"):
    with db.transaction(tenant) as connection:
        return connection.execute(
            select(base.usage_budgets.c.calls).where(base.usage_budgets.c.tenant == tenant)
        ).scalar_one()


def test_reservation_caps_usage_and_refunds_once(db):
    first = NativeAdmission.reserve(db, "tenant", 2)
    second = NativeAdmission.reserve(db, "tenant", 2)
    assert (first.reserved, second.reserved, used(db)) == (2, 1, 3)
    first.finish(1)
    first.finish(1)
    assert used(db) == 2
    with pytest.raises(ValueError, match="differently"):
        first.finish(0)
    with pytest.raises(ValueError, match="exceeds"):
        second.finish(2)
    second.finish(0)
    assert used(db) == 1


def test_unknown_dispatch_usage_keeps_the_reservation(db):
    with pytest.raises(RuntimeError):
        with NativeAdmission.reserve(db, "tenant", 3) as admission:
            admission.inflight = True
            raise RuntimeError("Query cancelled during native dispatch")
    assert used(db) == 3
    with db.transaction("tenant") as connection:
        row = connection.execute(select(native_admissions)).mappings().one()
    assert row["state"] == "UNCERTAIN" and row["requests"] is None
    assert NativeAdmission.reserve(db, "tenant", 1).reserved == 0


def test_failure_before_dispatch_refunds_and_tenants_are_separate(db):
    with pytest.raises(RuntimeError):
        with NativeAdmission.reserve(db, "tenant", 3):
            raise RuntimeError("Source validation failed")
    assert used(db) == 0
    with NativeAdmission.reserve(db, "other", 3) as other:
        other.requests = 2
    assert used(db, "other") == 2 and used(db) == 0
