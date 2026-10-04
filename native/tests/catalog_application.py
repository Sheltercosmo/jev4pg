"""Native evaluation over existing relations with caller privileges and live rows."""

from sqlalchemy import text


def verify_catalog_application(admin, service, catalog, tenant, observations):
    admin.execute("CREATE SCHEMA source_attachment")
    admin.execute(
        "CREATE TABLE source_attachment.notes(id uuid PRIMARY KEY,note text,p numeric,private text)"
    )
    admin.execute("""INSERT INTO source_attachment.notes VALUES
        ('00000000-0000-0000-0000-000000000001','完成',0.95,'not registered'),
        ('00000000-0000-0000-0000-000000000002','pending',0.05,'not registered')""")
    admin.execute("GRANT USAGE ON SCHEMA source_attachment TO native_application")
    admin.execute("GRANT SELECT ON source_attachment.notes TO native_application")
    attached = catalog.attach(tenant, "既有记录", "source_attachment", "notes", ["id", "note", "p"])
    checks = []
    start = len(observations)
    result = service.execute(
        tenant, """SELECT id FROM "既有记录" WHERE SEMANTIC(note,'完成了吗？')"""
    )
    assert result["result"] == [{"id": "00000000-0000-0000-0000-000000000001"}]
    assert result["manifest"]["execution_backend"] == "rust_postgresql"
    assert len(observations) == start + 2
    assert all("private" not in call["state"] for call in observations[start:])
    checks.append(
        "native semantic execution reads an attached PostgreSQL relation with UUID keys and only registered columns"
    )

    query = """WITH q AS (SELECT id,note,p FROM "既有记录")
        SELECT id FROM q WHERE SEMANTIC(note,'Does this describe finished work?') ORDER BY id"""
    result = service.execute(tenant, query)
    assert result["result"] == [{"id": "00000000-0000-0000-0000-000000000001"}]
    assert result["manifest"]["native_scheduler"] == "dependent_stage_dag"
    admin.execute("UPDATE source_attachment.notes SET note='ready',p=0.95 WHERE p<0.2")
    assert len(service.execute(tenant, query)["result"]) == 2
    checks.append(
        "attached source updates reach the dependent native DAG without reimport or Python population snapshots"
    )

    admin.execute("ALTER TABLE source_attachment.notes ALTER COLUMN p TYPE real")
    start = len(observations)
    try:
        service.execute(tenant, query)
    except ValueError as error:
        assert "schema changed" in str(error)
    else:
        raise AssertionError("Schema drift reached native inference")
    assert len(observations) == start
    catalog.detach(tenant, attached["id"])
    with service.db.transaction(tenant) as connection:
        assert (
            connection.execute(text("SELECT count(*) FROM source_attachment.notes")).scalar_one()
            == 2
        )
    checks.append(
        "schema changes stop native dispatch and detach preserves the existing source table"
    )
    return checks
