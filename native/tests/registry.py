"""Verify durable request ownership independently of provider transport."""

import hashlib
from concurrent.futures import ThreadPoolExecutor

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb


def key(value):
    return hashlib.sha256(value.encode()).hexdigest()


def claim(connection, identity, *, scope="registry-test", pool="test", daily=100, active=4):
    return connection.execute(
        "SELECT jev_native._registry_claim(%s,%s,%s,%s,%s,1000,3600,false)",
        (scope, key(identity), key(pool), active, daily),
    ).fetchone()[0]


def finish(connection, attempt, observation=None, failure=None):
    return connection.execute(
        "SELECT jev_native._registry_finish(%s,%s,%s)",
        (attempt, Jsonb(observation) if observation is not None else None, failure),
    ).fetchone()[0]


def verify_registry(
    connection, coordinator="native_coordinator", reader="registry_reader", *, dsn=None
):
    checks = []
    dsn = dsn or connection.info.dsn
    for role in (coordinator, reader):
        connection.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
        connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA jev_native TO {}").format(sql.Identifier(role))
        )
    for signature in (
        "_registry_lookup(text,text[],integer)",
        "_registry_claim(text,text,text,integer,integer,integer,integer,boolean)",
        "_registry_finish(uuid,jsonb,text)",
    ):
        connection.execute(
            sql.SQL("GRANT EXECUTE ON FUNCTION jev_native." + signature + " TO {}").format(
                sql.Identifier(coordinator)
            )
        )

    def concurrent_claim(_):
        with psycopg.connect(dsn, autocommit=True) as client:
            return claim(client, "one")

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(concurrent_claim, range(2)))
    assert sorted(row["state"] for row in results) == ["CLAIMED", "DISPATCHING"]
    attempt = results[0]["attempt"]
    assert results[1]["attempt"] == attempt
    observation = {"checked_fixture": "完成"}
    assert finish(connection, attempt, observation)
    assert finish(connection, attempt, observation)
    assert claim(connection, "one")["observation"] == observation
    assert (
        connection.execute(
            "SELECT active FROM jev_native.request_pools WHERE identity=%s", (key("test"),)
        ).fetchone()[0]
        == 0
    )
    checks.append("independent transactions admit one request and publication is idempotent")
    try:
        connection.execute(
            "UPDATE jev_native.evidence SET observation='{}' WHERE id=%s", (attempt,)
        )
    except psycopg.errors.RaiseException as error:
        assert "immutable" in str(error)
    else:
        raise AssertionError("Committed observations must not change in place")
    assert claim(connection, "one")["observation"] == observation
    checks.append("committed observations reject in-place changes even by their owner")

    first = claim(connection, "slot-a", pool="slots", active=1)
    assert claim(connection, "slot-b", pool="slots", active=1)["state"] == "SATURATED"
    assert finish(connection, first["attempt"], failure="UNCERTAIN")
    assert claim(connection, "slot-a", pool="slots", active=1)["state"] == "UNCERTAIN"
    assert claim(connection, "slot-b", pool="slots", active=1)["state"] == "SATURATED"
    connection.execute(
        "SELECT jev_native.reconcile_attempt(%s,'RETRY_ALLOWED','Provider confirmed request ended')",
        (first["attempt"],),
    )
    retry = claim(connection, "slot-a", pool="slots", active=1)
    assert retry["state"] == "CLAIMED" and retry["attempt"] != first["attempt"]
    assert not finish(connection, first["attempt"], observation)
    assert finish(connection, retry["attempt"], observation)
    checks.append(
        "uncertain requests retain admission and explicit reconciliation fences old attempts"
    )

    expired = claim(connection, "expired", pool="expiry", active=1)
    connection.execute(
        "UPDATE jev_native.request_attempts SET deadline=clock_timestamp()-interval '1 second' WHERE id=%s",
        (expired["attempt"],),
    )
    assert claim(connection, "expired", pool="expiry", active=1)["state"] == "UNCERTAIN"
    assert finish(connection, expired["attempt"], observation)
    assert claim(connection, "expired", pool="expiry", active=1)["state"] == "READY"
    checks.append(
        "lease expiry cannot authorize another charge but a current late receipt can settle"
    )

    budget = claim(connection, "quota-a", pool="quota", daily=1)
    assert finish(connection, budget["attempt"], failure="FAILED")
    assert claim(connection, "quota-a", pool="quota", daily=1)["state"] == "FAILED"
    assert claim(connection, "quota-b", pool="quota", daily=1)["state"] == "BLOCKED_BY_BUDGET"
    other = claim(connection, "quota-b", pool="quota", scope="other-scope", daily=1)
    assert other["state"] == "CLAIMED"
    assert finish(connection, other["attempt"], observation)
    checks.append("durable daily allowance counts failed dispatch and isolates authorized scopes")

    with psycopg.connect(dsn) as source:
        source.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        source.execute("SELECT count(*) FROM jev_native.evidence").fetchone()
        independent = claim(connection, "rollback")
        assert finish(connection, independent["attempt"], observation)
        assert (
            source.execute(
                "SELECT count(*) FROM jev_native.evidence WHERE id=%s", (independent["attempt"],)
            ).fetchone()[0]
            == 0
        )
        source.rollback()
    assert claim(connection, "rollback")["state"] == "READY"
    checks.append("control evidence commits independently of the source snapshot and rollback")

    for role, query in (
        (reader, "SELECT jev_native._registry_lookup('registry-test',ARRAY['x'],3600)"),
        (reader, "SELECT * FROM jev_native.evidence"),
        (coordinator, "DELETE FROM jev_native.evidence"),
        (coordinator, "SELECT jev_native.reconcile_attempt(gen_random_uuid(),'CLOSED','test')"),
    ):
        connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        try:
            connection.execute(query)
        except psycopg.errors.InsufficientPrivilege:
            pass
        else:
            raise AssertionError(
                "Registry permissions allowed direct evidence mutation or administration"
            )
        finally:
            connection.execute("RESET ROLE")
    checks.append("query roles cannot forge ledger evidence or administer request ownership")
    return checks
