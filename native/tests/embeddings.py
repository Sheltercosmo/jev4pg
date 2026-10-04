"""Native probability embeddings, parallel dispatch and local matrix operations."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb


def basis():
    return {
        "context_columns": ["body", "p"],
        "questions": {
            "action": {
                "type": "noul",
                "instructions": "Does this request action?",
                "subject_column": "body",
            },
            "resolved": {
                "type": "noul",
                "instructions": "Is the described work complete?",
                "subject_column": "body",
            },
        },
    }


def embed(connection, source, definition=None, options=None):
    return connection.execute(
        "SELECT ordinal,source,embedding,decisions,observation,usage,receipt FROM jev_native.embed(%s,%s,%s)",
        (source, Jsonb(definition or basis()), Jsonb(options or {})),
    ).fetchall()


def verify_embeddings(connection, observations, gates, must_fail):
    checks = []
    start = len(observations)
    rows = embed(connection, "SELECT 1 AS id,'Need a review' AS body,0.5 AS p")
    matrix = rows[0][2]
    assert matrix["probabilities"] == [[0.5, 0.5], [0.5, 0.5]]
    assert matrix["vector"] == [0.5] * 4 and matrix["complete"]
    assert [axis["question_id"] for axis in matrix["axes"]] == ["action", "resolved"]
    assert all(
        state["decision_state"] == "UNKNOWN" and state["output_state"] == "VALUE"
        for state in matrix["question_states"]
    )
    assert len(observations) == start + 1 and len(observations[-1]["questions"]) == 2
    assert set(observations[-1]["state"]) == {"body", "p"}
    checks.append(
        "native embeddings batch independent questions into one request and retain probabilities from uncertain categorical answers"
    )

    for note, prompt, source in [
        (
            "body",
            "Has the work finished?",
            "SELECT id,'Done' AS body,1.0 AS p FROM generate_series(1,3) id",
        ),
        (
            "说明",
            "任务是否已经完成？",
            "SELECT id,'工作已完成' AS 说明,1.0 AS p FROM generate_series(1,3) id",
        ),
    ]:
        definition = {
            "context_columns": [note, "p"],
            "questions": {"完成": {"type": "noul", "instructions": prompt, "subject_column": note}},
        }
        start = len(observations)
        rows = embed(connection, source, definition)
        assert [row[0] for row in rows] == [1, 2, 3] and len(observations) == start + 1
        assert all(row[2]["vector"] == [0.0, 1.0] for row in rows)
        assert max(row[5]["reused_rows"] for row in rows) == 2
    checks.append(
        "probability embeddings preserve duplicate rows and reuse identical projected contexts across English and Simplified Chinese contracts"
    )

    definition = {
        "context_columns": ["fixture_answers"],
        "questions": {
            "category": {
                "type": "choice",
                "instructions": "Choose a category",
                "criteria": {"a": "General", "b": "Review", "c": "Other"},
            },
            "urgency": {
                "type": "score",
                "instructions": "Rate urgency",
                "criteria": ["Low", "High"],
            },
        },
    }
    fixture = {
        "category": {
            "type": "choice",
            "choice": "b",
            "probabilities": {"c": 0.2, "b": 0.5, "a": 0.3},
        },
        "urgency": {
            "type": "score",
            "score": 0.75,
            "confidence": 0.1,
            "legend": {"0": "Low", "1": "High"},
            "probabilities": {"1": 0.75, "0": 0.25},
        },
    }
    source = "SELECT '" + json.dumps(fixture).replace("'", "''") + "'::jsonb AS fixture_answers"
    start = len(observations)
    row = embed(connection, source, definition, {"score_confidence_min": 0.8})[0]
    assert row[2]["probabilities"] == [[0.3, 0.5, 0.2], [0.25, 0.75]]
    assert row[2]["complete"] and len(observations) == start + 1
    assert all(state["decision_state"] == "UNKNOWN" for state in row[2]["question_states"])
    rebuilt = connection.execute(
        "SELECT jev_native.answer_matrix(%s,%s,%s)",
        (Jsonb(definition), Jsonb(row[3]), Jsonb(row[4]["evaluator"])),
    ).fetchone()[0]
    assert rebuilt == row[2] and len(observations) == start + 1
    checks.append(
        "Choice and Score retain their full ordered distributions and can rebuild a matrix from existing evidence without another request"
    )

    rows = embed(
        connection,
        "SELECT id,'Distance fixture' AS body,p FROM (VALUES(1,0.0),(2,0.5),(3,1.0)) valueset(id,p) ORDER BY id",
    )
    start = len(observations)
    connection.execute("CREATE TEMP TABLE embedding_distances(id integer, embedding jsonb)")
    for row in rows:
        connection.execute(
            "INSERT INTO embedding_distances VALUES(%s,%s)", (row[1]["id"], Jsonb(row[2]))
        )
    ranked = connection.execute(
        "SELECT id,jev_native.embedding_distance(embedding,%s) AS distance FROM embedding_distances ORDER BY distance,id",
        (Jsonb(rows[-1][2]),),
    ).fetchall()
    assert (
        [item[0] for item in ranked] == [3, 2, 1] and ranked[0][1] == 0.0 and ranked[-1][1] == 1.0
    )
    assert len(observations) == start
    changed = deepcopy(rows[-1][2])
    changed["evaluator"]["revision"] = "other-revision"
    must_fail(
        lambda: connection.execute(
            "SELECT jev_native.embedding_distance(%s,%s)", (Jsonb(rows[0][2]), Jsonb(changed))
        ),
        "same basis",
    )
    checks.append(
        "native embedding distance ranks stored distributions locally and rejects incompatible evaluator revisions"
    )

    start = len(observations)
    held = embed(connection, "SELECT 'Held' AS body,0.5 AS p", options={"max_requests": 0})[0][2]
    assert held["vector"] == [None] * 4 and held["output_state"] == "NOT_EVALUATED"
    assert all(state["operation_state"] == "BLOCKED_BY_BUDGET" for state in held["question_states"])
    null = embed(connection, "SELECT NULL::text AS body,0.5 AS p")[0][2]
    assert null["vector"] == [None] * 4 and all(
        state["operation_state"] == "SKIPPED" for state in null["question_states"]
    )
    assert embed(connection, "SELECT 'Empty' AS body,0.5 AS p WHERE false") == []
    assert len(observations) == start
    must_fail(
        lambda: connection.execute(
            "SELECT jev_native.embedding_distance(%s,%s)", (Jsonb(held), Jsonb(held))
        ),
        "Complete",
    )
    failed = embed(connection, "SELECT 'Bad response' AS body,1.2 AS p")[0][2]
    assert failed["vector"] == [None] * 4 and all(
        state["operation_state"] == "FAILED" for state in failed["question_states"]
    )
    checks.append(
        "budget holds, NULL subjects, malformed provider responses and empty populations remain distinct from zero probabilities"
    )

    start = len(observations)
    must_fail(lambda: embed(connection, "SELECT 1 AS p"), "context column is absent")
    must_fail(
        lambda: embed(
            connection,
            "SELECT 1 AS body,0.5 AS p",
            {"questions": basis()["questions"], "context_columns": ["p"]},
        ),
        "include every subject",
    )
    assert len(observations) == start
    connection.execute("SET ROLE native_reader")
    try:
        must_fail(lambda: embed(connection, "SELECT 1 AS body,0.5 AS p"), "permission denied")
    finally:
        connection.execute("RESET ROLE")
    connection.execute(
        "GRANT EXECUTE ON FUNCTION jev_native.embed(text,jsonb,jsonb) TO native_reader"
    )
    connection.execute("SET ROLE native_reader")
    try:
        assert len(embed(connection, "SELECT id,note AS body,p FROM work")) == 1
        must_fail(
            lambda: embed(connection, "SELECT private_note AS body,p FROM work"),
            "permission denied",
        )
    finally:
        connection.execute("RESET ROLE")
    checks.append(
        "embedding source projection, execution grants, column permissions and row security are enforced before dispatch"
    )

    gates["embedding-parallel"] = threading.Event()
    definition = basis()
    definition["context_columns"] = ["body", "p", "gate"]

    def parallel():
        with psycopg.connect(connection.info.dsn, autocommit=True) as worker:
            return embed(
                worker,
                "SELECT id::text AS body,0.5 AS p,'embedding-parallel' AS gate FROM generate_series(1,2) id",
                definition,
                {"concurrency": 2},
            )

    start = len(observations)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(parallel)
        try:
            deadline = time.monotonic() + 2
            while len(observations) < start + 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(observations) == start + 2 and not future.done()
        finally:
            gates["embedding-parallel"].set()
        assert len(future.result(timeout=5)) == 2
    with connection.transaction():
        before = connection.execute("SELECT count(*) FROM pg_cursors").fetchone()[0]
        connection.execute(
            "SELECT jev_native.embed('SELECT id::text AS body,0.5 AS p FROM generate_series(1,100) id',%s,'{\"batch_rows\":1}') LIMIT 1",
            (Jsonb(basis()),),
        ).fetchall()
        assert connection.execute("SELECT count(*) FROM pg_cursors").fetchone()[0] == before
    checks.append(
        "embedding contexts enter native dispatch concurrently and limited consumption closes source cursors"
    )

    from plans import stage, execute

    definition = basis()
    source = stage(
        "questions",
        "SELECT 1 AS id,'DAG projection' AS body,0.5 AS p",
        {"id": "integer", "body": "text", "p": "number"},
        semantic=True,
    )
    source["questions"] = definition["questions"]
    source["context_columns"] = definition["context_columns"]
    sql = (
        "SELECT id,jev_native.answer_matrix('"
        + json.dumps(definition).replace("'", "''")
        + "',__jev_decisions,__jev_observation->'evaluator') AS embedding FROM questions"
    )
    target = stage(
        "projected",
        sql,
        {"id": "integer", "embedding": "json"},
        inputs=[{"stage": "questions", "alias": "questions", "require_values": []}],
    )
    result = execute(connection, [source, target])
    assert result["rows"][0]["embedding"]["complete"] and result["usage"]["requests"] == 1
    checks.append(
        "native DAG projection consumes valid uncertain distributions without an extra semantic stage or completeness error"
    )

    example = Path(__file__).resolve().parents[2] / "examples/operators/native_embedding.sql"
    connection.execute(example.read_text(encoding="utf-8"))
    assert connection.execute("SELECT count(*) FROM example_embeddings").fetchone()[0] == 3
    checks.append(
        "documented native embedding and similarity SQL executes against the compiled extension"
    )
    definition = basis()
    definition["context_columns"] = ["body", "optional", "p"]
    definition["questions"]["resolved"]["subject_column"] = "optional"
    start = len(observations)
    partial = embed(
        connection, "SELECT 'Review requested' AS body,NULL::text AS optional,0.5 AS p", definition
    )[0]
    assert partial[2]["probabilities"] == [[0.5, 0.5], [None, None]]
    assert not partial[2]["complete"] and len(observations) == start + 1
    assert list(observations[-1]["questions"]) == ["action"]
    subset = deepcopy(definition)
    subset["questions"].pop("resolved")
    projected = connection.execute(
        "SELECT jev_native.answer_matrix(%s,%s,%s)",
        (Jsonb(subset), Jsonb(partial[3]), Jsonb(partial[4]["evaluator"])),
    ).fetchone()[0]
    assert projected["complete"] and projected["vector"] == [0.5, 0.5]
    assert len(observations) == start + 1
    checks.append(
        "an independently missing subject leaves other probabilities available and a smaller basis reuses the shared decisions without dispatch"
    )

    maximum = {
        "context_columns": ["body", "p"],
        "questions": {
            f"q{index:02}": {
                "type": "noul",
                "instructions": f"Independent criterion {index}",
                "subject_column": "body",
            }
            for index in range(32)
        },
    }
    start = len(observations)
    widest = embed(connection, "SELECT 'Bounded shared context' AS body,0.5 AS p", maximum)[0]
    assert len(widest[2]["vector"]) == 64 and widest[2]["complete"]
    assert len(observations) == start + 1 and len(observations[-1]["questions"]) == 32
    maximum["questions"]["too_many"] = maximum["questions"]["q00"]
    must_fail(lambda: embed(connection, "SELECT 'Unused' AS body,0.5 AS p", maximum), "1 to 32")
    assert len(observations) == start + 1
    checks.append(
        "the full 32-question basis shares one context request and oversized bases fail without truncation or provider work"
    )
    return checks


def verify_embedding_registry(connection, observations):
    source = "SELECT 1 AS id,'Reusable embedding' AS body,0.65 AS p"
    start = len(observations)
    first = embed(connection, source, options={"evidence_scope": "embedding-replay"})[0]
    second = embed(
        connection,
        source,
        options={
            "evidence_scope": "embedding-replay",
            "max_requests": 0,
            "max_judgments": 0,
            "accept": 0.6,
        },
    )[0]
    assert (
        first[2]["probabilities"] == second[2]["probabilities"]
        and first[2]["basis_id"] == second[2]["basis_id"]
    )
    assert first[3]["action"]["output_state"] == "UNKNOWN" and second[3]["action"]["value"] is True
    assert second[2]["complete"] and second[5]["durable_reused_rows"] == 1
    assert len(observations) == start + 1
    return [
        "native embeddings reuse durable observations across queries and decision policies with zero new-call allowance"
    ]
