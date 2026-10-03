"""Durable request reservations shared with the existing tenant daily allowance."""

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import insert, select, update

from .. import schema as base
from ..ledger import digest, now, uid
from .schema import native_admissions


@dataclass
class NativeAdmission:
    db: object
    tenant: str
    identity: str
    reserved: int
    requests: int = 0
    inflight: bool = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.finish(None if self.inflight else self.requests)

    @classmethod
    def reserve(cls, db, tenant, requested):
        if not 0 <= requested <= 1000:
            raise ValueError("Invalid native request reservation")
        daily = int(os.getenv("SDD_DAILY_EVALUATIONS", "1000"))
        if daily < 0:
            raise ValueError("Daily evaluation allowance cannot be negative")
        day = str(datetime.now(timezone.utc).date())
        budget_id = digest([tenant, day])
        identity = uid()
        upsert = __import__(
            "sqlalchemy.dialects." + db.engine.dialect.name, fromlist=["insert"]
        ).insert
        with db.transaction(tenant) as connection:
            connection.execute(
                upsert(base.usage_budgets)
                .values(
                    id=budget_id,
                    tenant=tenant,
                    day=day,
                    calls=0,
                )
                .on_conflict_do_nothing()
            )
            used = connection.execute(
                select(base.usage_budgets.c.calls)
                .where(
                    base.usage_budgets.c.id == budget_id,
                    base.usage_budgets.c.tenant == tenant,
                )
                .with_for_update()
            ).scalar_one()
            reserved = min(requested, max(0, daily - used))
            connection.execute(
                update(base.usage_budgets)
                .where(
                    base.usage_budgets.c.id == budget_id,
                    base.usage_budgets.c.tenant == tenant,
                )
                .values(calls=base.usage_budgets.c.calls + reserved)
            )
            connection.execute(
                insert(native_admissions).values(
                    id=identity,
                    tenant=tenant,
                    budget_id=budget_id,
                    reserved=reserved,
                    requests=None,
                    state="RESERVED",
                    created_at=now(),
                )
            )
        return cls(db, tenant, identity, reserved)

    def finish(self, requests):
        """Unknown dispatch counts retain their reservation; settlement is idempotent."""
        if requests is not None and not 0 <= requests <= self.reserved:
            raise ValueError("Native request usage exceeds its reservation")
        with self.db.transaction(self.tenant) as connection:
            reservation = (
                connection.execute(
                    select(native_admissions)
                    .where(
                        native_admissions.c.id == self.identity,
                        native_admissions.c.tenant == self.tenant,
                    )
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if reservation["state"] != "RESERVED":
                if reservation["requests"] != requests:
                    raise ValueError("Native request reservation was already settled differently")
                return
            if requests is not None:
                refund = reservation["reserved"] - requests
                updated = connection.execute(
                    update(base.usage_budgets)
                    .where(
                        base.usage_budgets.c.id == reservation["budget_id"],
                        base.usage_budgets.c.tenant == self.tenant,
                        base.usage_budgets.c.calls >= refund,
                    )
                    .values(calls=base.usage_budgets.c.calls - refund)
                )
                if updated.rowcount != 1:
                    raise ValueError("Native allowance accounting is inconsistent")
            connection.execute(
                update(native_admissions)
                .where(
                    native_admissions.c.id == self.identity,
                    native_admissions.c.tenant == self.tenant,
                )
                .values(requests=requests, state="SETTLED" if requests is not None else "UNCERTAIN")
            )
