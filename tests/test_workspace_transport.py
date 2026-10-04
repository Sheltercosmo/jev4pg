"""Fresh transport checks after runtime freeze; retained for future regressions."""

from test_generic_api import api as api


def test_large_key_survives_reviewed_write_and_history(api):
    client, headers, _ = api
    created = client.post(
        "/datasets",
        headers=headers,
        json={
            "name": "库存",
            "rows": [{"编号": 9007199254740995, "数量": 2}, {"编号": 9007199254740997, "数量": 3}],
            "primary_key": ["编号"],
        },
    )
    assert created.status_code == 200
    preview = client.post(
        "/data/sql",
        headers=headers,
        json={"sql": 'UPDATE "库存" SET "数量"=7 WHERE "编号"=9007199254740995'},
    ).json()
    assert preview["before_sample"][0]["编号"] == "9007199254740995"
    commit = client.post(f"/data/mutations/{preview['preview_token']}/commit", headers=headers)
    assert commit.status_code == 200 and commit.json()["manifest"]["committed"]
    page = client.post(
        f"/datasets/{created.json()['id']}/scan", headers=headers, json={"limit": 1}
    ).json()
    assert page["result"] == [{"编号": "9007199254740995", "数量": 7}]
    after = client.post(
        f"/datasets/{created.json()['id']}/scan",
        headers=headers,
        json={"after": page["next_after"]},
    ).json()
    assert after["result"] == [{"编号": "9007199254740997", "数量": 3}]


def test_nested_json_integer_boundary_and_boolean(api):
    client, headers, _ = api
    nested = {
        "positive": 2**63 - 1,
        "negative": -(2**63),
        "flag": False,
        "array": [2**53 - 1, 2**53],
    }
    created = client.post(
        "/datasets",
        headers=headers,
        json={"name": "Packets", "rows": [{"id": 1, "payload": nested}], "primary_key": ["id"]},
    ).json()
    result = client.post(f"/datasets/{created['id']}/scan", headers=headers, json={}).json()[
        "result"
    ][0]["payload"]
    assert result == {
        "positive": "9223372036854775807",
        "negative": "-9223372036854775808",
        "flag": False,
        "array": [2**53 - 1, str(2**53)],
    }


def test_required_csv_cell_is_held_without_creating_table(api):
    client, headers, _ = api
    body = {
        "name": "required_records",
        "content": 'key,value\n1,""\n',
        "columns": [
            {"name": "key", "type": "integer", "nullable": False},
            {"name": "value", "type": "text", "nullable": False},
        ],
    }
    preview = client.post("/datasets/csv/preview", headers=headers, json=body).json()
    assert not preview["valid"] and preview["errors"][0]["column"] == "value"
    attempt = client.post(
        "/datasets/csv", headers=headers, json={**body, "fingerprint": preview["fingerprint"]}
    )
    assert attempt.status_code == 400
    assert not any(
        dataset["name"] == body["name"]
        for dataset in client.get("/datasets", headers=headers).json()["datasets"]
    )
