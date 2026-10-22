from sqlalchemy import event, insert

from sdd.operators import schema
from sdd.operators.budget import token_bound
from sdd.operators.runtime import Runtime
from sdd.operators.store import Store
from sdd.operators.types import WorkItem
from test_operator_runtime import operators as operator_fixture

operators = operator_fixture


def test_bulk_evidence_selects_latest_valid_row_per_key_and_isolates_tenants(operators):
    db, _, service = operators
    store = service.store
    values = dict(
        model="fixture",
        subject_id="s",
        source_revisions=["v1"],
        question={"type": "noul", "instructions": "完成了吗？"},
        context_hash="context",
        hypothesis={},
        answer={"type": "noul", "noul": 0.5},
    )
    keys = [f"k{i}" for i in range(600)]
    with db.transaction(store.tenant) as connection:
        for key in keys:
            store.add(schema.observations, connection=connection, cache_key=key, **values)
        for identity in ("older", "tie-a", "tie-b", "invalid"):
            connection.execute(
                insert(schema.observations).values(
                    **values,
                    id=identity,
                    tenant=store.tenant,
                    cache_key="history",
                    created_at={"older": "2026-01-01", "invalid": "2026-03-01"}.get(
                        identity, "2026-02-01"
                    ),
                )
            )
        store.add(
            schema.invalidations,
            connection=connection,
            observation_id="invalid",
            reason="revision changed",
        )
    other = Store(db, "other-tenant", "u")
    other.add(schema.observations, cache_key="history", **values)
    statements = []

    def count(_, __, statement, *args):
        if (
            statement.lstrip().upper().startswith("SELECT")
            and "jev_operator_observations" in statement
        ):
            statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", count)
    try:
        found = store.cached_many([*keys, "history", "absent", *keys])
        assert len(statements) == 3
        assert len(found) == 601 and found["history"]["id"] == "tie-b"
        assert {row["tenant"] for row in found.values()} == {store.tenant}
        assert store.cached_many([]) == {}
        assert len(statements) == 3
        assert store.cached("history") == found["history"]
        assert other.cached("history")["tenant"] == "other-tenant"
    finally:
        event.remove(db.engine, "before_cursor_execute", count)


def test_incremental_packing_matches_full_utf8_payload_bounds(operators):
    _, model, service = operators
    runtime = Runtime(service.store, model, "unused")
    for state in (
        {"字段": "中" * 4000, "nullable": None},
        {"id": 9007199254740993, "text": 'a"\\b'},
    ):
        items = [
            WorkItem(
                str(i), state, {"type": "noul", "instructions": "判定" * (1 + i * 40)}, "s", ("v1",)
            )
            for i in range(67)
        ]
        aliases = {runtime.key(item): [item.id] for item in items}
        results = {}
        actual = runtime._pack_batches(items, aliases, results)
        expected, current = [], []
        for item in items:
            one = token_bound({"state": runtime.state(item), "question": item.question})
            if one > 32000:
                assert results[item.id].operation_state == "BLOCKED_BY_POLICY"
                continue
            candidate = [*current, item]
            if current and (len(candidate) > 32 or runtime.payload_tokens(candidate) > 64000):
                expected.append(current)
                current = []
            current.append(item)
        if current:
            expected.append(current)
        assert [[i.id for i in b] for b in actual] == [[i.id for i in b] for b in expected]
        assert all(runtime.payload_tokens(batch) <= 64000 for batch in actual)


def test_warm_operator_stage_uses_bulk_reuse_and_preserves_aliases(operators):
    _, model, service = operators
    items = [
        {
            "id": str(i),
            "subject_id": "shared",
            "state": {"说明": "已完成"},
            "question": {"type": "noul", "instructions": f"任务 {i % 4} 已完成"},
        }
        for i in range(20)
    ]
    first = service.call("EVALUATE", {"work_items": items})
    calls = model.calls
    lookups = []
    original = service.store.cached_many

    def lookup(keys):
        lookups.append(list(keys))
        return original(keys)

    service.store.cached_many = lookup
    warm = service.call("EVALUATE", {"work_items": items}, limits={"max_requests": 0})
    assert model.calls == calls == 1
    assert len(lookups) == 1 and len(lookups[0]) == 20
    assert warm["manifest"]["reserved_requests"] == 0
    assert warm["value"].keys() == first["value"].keys()
    for key, decision in warm["value"].items():
        assert decision["cached"] is True
        assert {k: v for k, v in decision.items() if k != "cached"} == {
            k: v for k, v in first["value"][key].items() if k != "cached"
        }
