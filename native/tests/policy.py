"""Decision policy parity and raw-observation replay through real SQL calls."""

from dataclasses import asdict

from psycopg.types.json import Jsonb

from sdd.operators.types import Policy


def verify_policy(connection, observations):
    checks = []
    for selected, label in (("unknown", "Insufficient evidence"), ("未知", "信息不足")):
        question = {
            "type": "choice",
            "instructions": "Choose the supported outcome. 选择有依据的结果。",
            "criteria": {selected: label, "yes": "Supported"},
        }
        answer = {
            "type": "choice",
            "choice": selected,
            "probabilities": {selected: 0.9, "yes": 0.1},
        }
        policy = Policy(unknown_options=(selected,), revision="review-policy-v2")
        options = asdict(policy)
        options["policy_revision"] = options.pop("revision")
        source, decisions, observation, applied = connection.execute(
            "SELECT source,decisions,observation,policy FROM jev_native.scan("
            "'SELECT ' || quote_literal(%s::jsonb::text) || '::jsonb AS fixture_answers',%s,%s)",
            (Jsonb({"q": answer}), Jsonb({"q": question}), Jsonb(options)),
        ).fetchone()
        assert decisions["q"]["output_state"] == policy.resolve(answer).output_state == "UNKNOWN"
        assert applied == {**asdict(policy), "unknown_options": list(policy.unknown_options)}
        start = len(observations)
        applied["unknown_options"] = []
        applied["revision"] = "category-policy-v3"
        replayed = connection.execute(
            "SELECT jev_native.decide(%s,%s,%s)",
            (Jsonb(source), Jsonb(observation), Jsonb(applied)),
        ).fetchone()[0]
        assert replayed["q"]["value"] == selected and len(observations) == start
    checks.append(
        "explicit Choice uncertainty policy matches declared English and Chinese sentinel IDs"
    )
    checks.append(
        "SQL policy receipts replay category choices from unchanged observations without calls"
    )

    question = {"type": "score", "instructions": "Rate urgency.", "criteria": ["Normal", "Urgent"]}
    answer = {
        "type": "score",
        "score": 0.7,
        "confidence": 0.4,
        "probabilities": {"0": 0.3, "1": 0.7},
        "legend": {"0": "Normal", "1": "Urgent"},
    }
    policy = Policy(score_confidence_min=0.6)
    source, observation, decisions = connection.execute(
        "SELECT source,observation,decisions FROM jev_native.scan("
        "'SELECT ' || quote_literal(%s::jsonb::text) || '::jsonb AS fixture_answers',%s,%s)",
        (Jsonb({"q": answer}), Jsonb({"q": question}), Jsonb({"score_confidence_min": 0.6})),
    ).fetchone()
    assert decisions["q"]["output_state"] == policy.resolve(answer).output_state == "UNKNOWN"
    replayed = connection.execute(
        "SELECT jev_native.decide(%s,%s,%s)",
        (Jsonb(source), Jsonb(observation), Jsonb({"score_confidence_min": 0.4})),
    ).fetchone()[0]
    assert replayed["q"]["value"] == 0.7
    checks.append(
        "Score confidence thresholds apply during native execution and local policy replay"
    )
    return checks


def verify_registry_policy(connection, observations):
    questions = {
        "route": {
            "type": "choice",
            "instructions": "Choose.",
            "criteria": {"unknown": "Unresolved", "yes": "Supported"},
        },
        "priority": {"type": "score", "instructions": "Rate.", "criteria": ["Normal", "Urgent"]},
    }
    answers = {
        "route": {
            "type": "choice",
            "choice": "unknown",
            "probabilities": {"unknown": 0.9, "yes": 0.1},
        },
        "priority": {
            "type": "score",
            "score": 0.7,
            "confidence": 0.4,
            "probabilities": {"0": 0.3, "1": 0.7},
            "legend": {"0": "Normal", "1": "Urgent"},
        },
    }
    sql = (
        "SELECT decisions,observation,receipt,usage,policy FROM jev_native.scan("
        "'SELECT ' || quote_literal(%s::jsonb::text) || '::jsonb AS fixture_answers',%s,%s)"
    )
    start = len(observations)
    first = connection.execute(
        sql, (Jsonb(answers), Jsonb(questions), Jsonb({"evidence_scope": "typed-policy"}))
    ).fetchone()
    assert all(answer["output_state"] == "VALUE" for answer in first[0].values())
    policy = {
        "evidence_scope": "typed-policy",
        "unknown_options": ["unknown"],
        "score_confidence_min": 0.6,
        "policy_revision": "review-v2",
        "max_requests": 0,
    }
    reused = connection.execute(sql, (Jsonb(answers), Jsonb(questions), Jsonb(policy))).fetchone()
    assert all(answer["output_state"] == "UNKNOWN" for answer in reused[0].values())
    assert reused[1] == first[1] and reused[2]["attempt_id"] == first[2]["attempt_id"]
    assert reused[2]["storage_state"] == "REUSED" and reused[3]["requests"] == 0
    assert reused[4]["revision"] == "review-v2" and len(observations) == start + 1
    return ["durable replay applies full Choice and Score policies without new provider admission"]
