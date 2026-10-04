from types import SimpleNamespace

import pytest

from sdd.db import Database
from sdd.generic.catalog import Catalog
from sdd.generic.hybrid_feedback import observed_context
from sdd.generic.planning_review import catalog_signature
from sdd.generic.planning_samples import PlanningSamples, literal_terms, mentioned


@pytest.fixture
def source(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "samples.db"))
    db.initialize()
    catalog = Catalog(db)
    dataset = catalog.create(
        "a",
        "notes",
        [
            {"id": 1, "code": "待处理", "note": "x" * 400, "value": None},
            {"id": 2, "code": "已完成", "note": "", "value": 2},
            {"id": 3, "code": "待处理", "note": None, "value": 3},
        ],
        primary_key=["id"],
    )
    yield db, catalog, dataset
    db.engine.dispose()


def test_exact_values_do_not_include_truncated_text(source):
    _, catalog, dataset = source
    samples = PlanningSamples(catalog)
    batch = samples.sample("a", dataset)
    assert batch.complete
    evidence = batch.evidence("note")
    assert len(evidence["examples"][0]) == 160
    assert evidence["exact_values"] == [""]
    assert not evidence["complete"] and evidence["text_may_be_truncated"]
    assert evidence["observed_nulls"] == 1 and evidence["observed_blanks"] == 1
    assert batch.evidence("code")["exact_values"] == ["待处理", "已完成"]


def test_caps_cache_and_states(source, monkeypatch):
    _, catalog, dataset = source
    samples = PlanningSamples(catalog, row_limit=2, read_limit=1)
    batch = samples.sample("a", dataset)
    assert len(batch.rows) == 2 and batch.operation_state == "TRUNCATED"
    assert samples.column("a", dataset, "code") is batch and samples.reads == 1
    assert samples.unique("a", dataset, "id") is None
    other = catalog.create("a", "other", [{"code": "unseen"}])
    skipped = samples.sample("a", other)
    assert (
        skipped.output_state == "NOT_EVALUATED" and skipped.operation_state == "BLOCKED_BY_BUDGET"
    )
    assert batch.evidence("unread")["output_state"] == "NOT_EVALUATED"
    tiny = PlanningSamples(catalog, byte_limit=3).sample("a", dataset)
    assert tiny.rows == [] and not tiny.complete
    monkeypatch.setattr(
        catalog,
        "table",
        lambda *a, **kw: (_ for _ in ()).throw(ValueError("private source failure")),
    )
    failed = PlanningSamples(catalog).sample("a", dataset)
    assert failed.output_state == "UNKNOWN" and failed.operation_state == "FAILED"
    assert "private" not in str(failed.evidence("code"))


def test_uniqueness_uses_database_comparison(source):
    db, catalog, _ = source
    dataset = catalog.create("a", "casefold", [{"code": "A"}], primary_key=["code"])
    with db.transaction("a") as conn:
        conn.exec_driver_sql(f'DROP TABLE "{dataset["table_name"]}"')
        conn.exec_driver_sql(f'CREATE TABLE "{dataset["table_name"]}" (code TEXT COLLATE NOCASE)')
        conn.exec_driver_sql(f"INSERT INTO \"{dataset['table_name']}\" VALUES ('A'),('a')")
    assert PlanningSamples(catalog).unique("a", dataset, "code") is False


@pytest.mark.parametrize(
    "question,value",
    [
        ("Show North Harbor records", "North Harbor"),
        ("Please list records for north harbor", "North Harbor"),
        ("列出星河站的记录", "星河站"),
        ("查看“已完成”状态的工单", "已完成"),
        ("Find 'O’Connor Park'", "O’Connor Park"),
        ("列出North Harbor的记录", "North Harbor"),
        ("列出站3号的记录", "站3号"),
    ],
)
def test_literal_hypotheses_and_mentions(question, value):
    assert value in literal_terms(question)
    assert mentioned(question, value)


def test_catalog_review_signature_ignores_samples(source):
    _, catalog, _ = source
    sampled = catalog.model_catalog("a")
    structural = catalog.model_catalog("a", include_values=False)
    assert catalog_signature(sampled) == catalog_signature(structural)
    assert sampled[0]["columns"][1]["value_evidence"]["sample_only"]


def test_hybrid_field_budget_is_explicit(source):
    _, catalog, _ = source
    dataset = catalog.create("a", "wide", [{f"c{i}": i for i in range(30)}])
    packet = {"request": "Show records", "catalog": [dataset]}
    output, trace = observed_context("a", packet, catalog)
    assert output["catalog"][0]["columns"][24]["value_evidence"]["output_state"] == "NOT_EVALUATED"
    assert output["catalog"][0]["columns"][0]["value_evidence"]["examples"] == [0]
    assert trace["sample_only"]


def test_partial_domain_keeps_explicit_user_literal(source, monkeypatch):
    from sdd.generic.compositional import CompositionalPlanner
    from sdd.generic.parallel_planner import ParallelPlanner
    from sdd.generic.relational import Field

    _, catalog, _ = source
    planner = ParallelPlanner(catalog, SimpleNamespace(model="local-test"))
    planner.request = "Find explicitly supplied new code"
    planner.observed["notes", "code"] = {"old"}
    planner.observed_complete["notes", "code"] = False
    monkeypatch.setattr(CompositionalPlanner, "ground_value", lambda *args, **kw: args[4][-1])
    field = Field("f0", "notes", "code", "text")
    assert planner.ground_value("a", {}, field, ["old", "new"], "value") == "new"


def test_parallel_batch_shares_capacity_without_first_table_starvation(source):
    _, catalog, dataset = source
    other = catalog.create("a", "small", [{"code": "important"}])
    samples = PlanningSamples(catalog, byte_limit=240)
    first, second = samples.many("a", [(dataset, ["note"]), (other, ["code"])])
    assert first.operation_state == "TRUNCATED" and not first.complete
    assert second.evidence("code")["exact_values"] == ["important"]
    assert samples.bytes <= 240


def test_partial_dictionary_is_evidence_not_an_unresolved_objective(source):
    from sdd.generic.parallel_planner import ParallelPlanner

    _, catalog, dataset = source
    planner = ParallelPlanner(catalog, SimpleNamespace(model="local-test"))
    planner.request = "Show 待处理 records"
    planner.samples = PlanningSamples(catalog, row_limit=1)
    assert "待处理" in planner.domain("a", dataset, "code")
    assert not planner.issues
    assert planner.graph["value_sources"][0]["complete"] is False
