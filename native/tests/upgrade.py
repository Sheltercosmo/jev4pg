"""Upgrade the published SQL schema without replacing its stored registry data."""

import secrets

import psycopg
from sqlalchemy import URL

from sdd.bootstrap import migrate


def verify_upgrade(admin):
    admin.execute("CREATE DATABASE native_upgrade")
    password = secrets.token_urlsafe(32)
    connection = psycopg.connect(admin.info.dsn, dbname="native_upgrade", autocommit=True)
    try:
        connection.execute("CREATE EXTENSION jev_native VERSION '0.1.0'")
        assert connection.execute(
            "SELECT extconfig IS NULL FROM pg_extension WHERE extname='jev_native'"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO jev_native.evidence(id,scope,identity,observation) VALUES "
            "(gen_random_uuid(),'upgrade','stored', '{\"marker\":\"保留原始证据\"}')"
        )
        connection.execute("SELECT setval('jev_native.evidence_sequence_seq',100)")
        connection.execute("CREATE ROLE native_upgrade_reader")
        connection.execute("GRANT USAGE ON SCHEMA jev_native TO native_upgrade_reader")
        connection.execute(
            "GRANT EXECUTE ON FUNCTION jev_native.embed(text,jsonb,jsonb) TO native_upgrade_reader"
        )
        url = URL.create(
            "postgresql+psycopg",
            username=connection.info.user,
            host=connection.info.host,
            port=connection.info.port,
            database="native_upgrade",
        )
        migrate(url, "native_upgrade_app", password, native_interface=True)
        migrate(url, "native_upgrade_app", native_interface=True)
        version, relations = connection.execute(
            "SELECT extversion, cardinality(extconfig) FROM pg_extension WHERE extname='jev_native'"
        ).fetchone()
        assert version == "0.2.0" and relations == 6
        assert (
            connection.execute("SELECT observation->>'marker' FROM jev_native.evidence").fetchone()[
                0
            ]
            == "保留原始证据"
        )
        assert (
            connection.execute("SELECT nextval('jev_native.evidence_sequence_seq')").fetchone()[0]
            == 101
        )
        assert connection.execute(
            "SELECT has_function_privilege('native_upgrade_reader','jev_native.embed(text,jsonb,jsonb)','EXECUTE')"
        ).fetchone()[0]
    finally:
        connection.close()
        admin.execute("DROP DATABASE native_upgrade WITH (FORCE)")
        admin.execute("DROP ROLE IF EXISTS native_upgrade_reader,native_upgrade_app")
    return [
        "Published native 0.1.0 schema upgrades to 0.2.0 without losing evidence, sequence state or client grants"
    ]
