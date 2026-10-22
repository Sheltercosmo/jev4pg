"""PostgreSQL contracts for bounded record-map windows and shared evidence."""

from psycopg.types.json import Jsonb


QUESTIONS = {
    "done": {
        "type": "noul",
        "instructions": "Has the work finished? 完成了吗？",
        "subject_column": "note",
    }
}


def mapped(connection, source, options=None, columns=None, questions=None):
    return connection.execute(
        "SELECT window_ordinal,rows,evidence,usage,policy FROM jev_native.scan_mapped(%s,%s,%s,%s)",
        (
            source,
            Jsonb(questions or QUESTIONS),
            Jsonb(columns if columns is not None else {"done": ["note", "p"]}),
            Jsonb(options or {}),
        ),
    ).fetchall()


def verify_record_maps(connection, observations, must_fail):
    checks = []
    source = """SELECT id, CASE WHEN id%4=0 THEN NULL ELSE '工作进度' END AS note,
      CASE id%4 WHEN 1 THEN 0.95 WHEN 2 THEN 0.05 ELSE 0.5 END AS p
      FROM generate_series(1,128) id ORDER BY id"""
    start = len(observations)
    windows = mapped(connection, source, {"map_window_rows": 32})
    rows = [row for window in windows for row in window[1]]
    assert len(windows) == 4 and [row["ordinal"] for row in rows] == list(range(1, 129))
    assert len(observations) == start + 1
    assert rows[0]["decisions"]["done"]["value"] is True
    assert rows[1]["decisions"]["done"]["value"] is False
    assert rows[2]["decisions"]["done"]["output_state"] == "UNKNOWN"
    assert rows[3]["decisions"]["done"]["output_state"] == "NOT_EVALUATED"
    for window in windows:
        evidence = window[2]
        assert evidence["format_version"] == 1 and len(evidence["observations"]) == 1
        assert len(evidence["bindings"]) == 24 and evidence["receipts"] == []
        for binding in evidence["bindings"]:
            observation = evidence["observations"][binding["observation"]]
            record = observation["questions"][binding["provider_question"]]["instructions"][
                "record"
            ]
            original = window[1][binding["ordinal"]]["source"]
            assert record == {key: original[key] for key in ["note", "p"]}
            assert "id" not in record
    assert windows[-1][3]["executor"]["requests"] == 1
    assert windows[-1][3]["window"]["reused_judgments"] == 24
    checks.append(
        "mapped SQL preserves Unicode, null eligibility, duplicate rows and cross-window evidence bindings"
    )

    start = len(observations)
    mapped(connection, source, {"map_window_rows": 32})
    assert len(observations) == start + 1
    checks.append("mapped in-process cache is invocation-local and cannot leak between SQL callers")

    options = {"map_window_rows": 2, "map_cache_bytes": 0}
    start = len(observations)
    mapped(connection, "SELECT id,'完成' AS note,1 AS p FROM generate_series(1,8) id", options)
    assert len(observations) == start + 4
    checks.append("disabling the mapped cache still deduplicates within each bounded source window")

    start = len(observations)
    windows = mapped(
        connection,
        "SELECT id,'done' AS note,0.95 AS p FROM generate_series(1,80) id ORDER BY id",
        {"max_requests": 1},
        columns={},
    )
    assert len(observations) == start + 1
    rows = windows[0][1]
    assert len(rows) == 80
    assert sum(row["decisions"]["done"]["output_state"] == "VALUE" for row in rows) == 32
    assert (
        sum(row["decisions"]["done"].get("operation_state") == "BLOCKED_BY_BUDGET" for row in rows)
        == 48
    )
    checks.append("one query budget covers every mapped request and blocked rows remain visible")

    start = len(observations)
    must_fail(lambda: mapped(connection, source, {"max_rows": 3}), "max_rows")
    must_fail(lambda: mapped(connection, source, {"map_window_rows": 513}), "map_window_rows")
    must_fail(lambda: mapped(connection, source, columns={"done": ["absent"]}), "subject")
    must_fail(lambda: mapped(connection, source, columns={"done": ["note", "absent"]}), "absent")
    must_fail(
        lambda: mapped(
            connection, "SELECT NULL AS note,0 AS p WHERE false", columns={"missing": ["note"]}
        ),
        "existing questions",
    )
    assert len(observations) == start
    assert mapped(connection, "SELECT NULL AS note,0 AS p WHERE false") == []
    checks.append(
        "mapped source limits and projection contracts fail before provider dispatch, including empty sources"
    )

    connection.execute("CREATE ROLE mapped_reader")
    connection.execute("GRANT USAGE ON SCHEMA jev_native TO mapped_reader")
    connection.execute("SET ROLE mapped_reader")
    try:
        must_fail(lambda: mapped(connection, "SELECT 'done' AS note,1 AS p"), "permission")
    finally:
        connection.execute("RESET ROLE")
    connection.execute(
        "GRANT EXECUTE ON FUNCTION jev_native.scan_mapped(text,jsonb,jsonb,jsonb) TO mapped_reader"
    )
    connection.execute("SET ROLE mapped_reader")
    try:
        must_fail(lambda: mapped(connection, "SELECT note,p FROM work"), "permission denied")
        assert mapped(connection, "SELECT 'done' AS note,1 AS p")[0][1][0]["decisions"]["done"][
            "value"
        ]
    finally:
        connection.execute("RESET ROLE")
    checks.append("mapped SQL is denied to PUBLIC and reads source data with invoker privileges")
    connection.execute("CREATE TABLE mapped_tenants(tenant text,note text,p numeric)")
    connection.execute("INSERT INTO mapped_tenants VALUES ('mapped_reader','visible',0.95),('other','private',0.05)")
    connection.execute("ALTER TABLE mapped_tenants ENABLE ROW LEVEL SECURITY")
    connection.execute("CREATE POLICY mapped_scope ON mapped_tenants USING (tenant = current_user)")
    connection.execute("GRANT SELECT ON mapped_tenants TO mapped_reader")
    connection.execute("SET ROLE mapped_reader")
    try:
        scoped = mapped(connection, "SELECT note,p FROM mapped_tenants")
        assert len(scoped) == 1 and len(scoped[0][1]) == 1
        assert scoped[0][1][0]["source"]["note"] == "visible"
        assert len(scoped[0][2]["observations"]) == 1
        sent = scoped[0][2]["observations"][0]["questions"]
        assert len(sent) == 1 and next(iter(sent.values()))["instructions"]["record"]["note"] == "visible"
    finally:
        connection.execute("RESET ROLE")
    checks.append("mapped source rows and provider context both respect row-level security")
    return checks


def verify_mapped_registry(connection, observations):
    source = "SELECT id,'工作完成' AS note,0.7 AS p FROM generate_series(1,4) id ORDER BY id"
    options = {"map_window_rows": 2, "evidence_scope": "mapped-receipts"}
    start = len(observations)
    first = mapped(connection, source, options)
    assert len(observations) == start + 1
    assert all(
        row["decisions"]["done"]["output_state"] == "UNKNOWN"
        for window in first
        for row in window[1]
    )
    assert all(
        receipt["receipt"]["storage_state"] == "STORED"
        for window in first
        for receipt in window[2]["receipts"]
    )
    assert all(window[2]["receipts"] for window in first)
    second = mapped(connection, source, {**options, "accept": 0.6, "max_requests": 0})
    assert len(observations) == start + 1
    assert all(row["decisions"]["done"]["value"] for window in second for row in window[1])
    assert all(
        receipt["receipt"]["storage_state"] == "REUSED"
        for window in second
        for receipt in window[2]["receipts"]
    )
    assert second[-1][3]["executor"]["requests"] == 0
    return [
        "mapped SQL retains durable receipts through window reuse and replays threshold changes without provider allowance"
    ]
