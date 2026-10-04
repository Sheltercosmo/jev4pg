import sqlite3

import pytest
from sqlglot import parse_one
from sqlglot.optimizer.qualify import qualify

from sdd.generic.query_rewrites import decorrelate_scalar_aggregate


def rewrite(query):
    return decorrelate_scalar_aggregate(qualify(parse_one(query, dialect="postgres")))


@pytest.mark.parametrize(
    "aggregate", ["AVG(b.value)*2", "SUM(b.value)", "MIN(b.value)", "MAX(b.value)+1"]
)
def test_correlated_aggregate_preserves_nulls_empty_matches_and_duplicates(aggregate):
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE readings (id INTEGER, bucket INTEGER, category TEXT, value REAL)"
    )
    connection.executemany(
        "INSERT INTO readings VALUES (?, ?, ?, ?)",
        [
            (1, 1, "a", 2),
            (2, 1, "a", 8),
            (3, 1, "b", 30),
            (4, 2, "a", None),
            (5, None, "a", 5),
            (6, None, "a", 10),
            (7, 3, None, 7),
            (8, 3, None, 7),
        ],
    )
    query = f"SELECT a.id, (SELECT {aggregate} FROM readings b WHERE b.bucket=a.bucket AND b.category=a.category) AS x FROM readings a ORDER BY a.id"
    changed = rewrite(query).sql(dialect="sqlite")
    assert "JOIN" in changed
    assert connection.execute(changed).fetchall() == connection.execute(query).fetchall()
    connection.close()


@pytest.mark.parametrize(
    "expression, suffix",
    [
        ("COUNT(*)", ""),
        ("COALESCE(SUM(b.value),0)", ""),
        ("AVG(b.value)", " AND b.value>0"),
        ("AVG(b.value)", " LIMIT 1"),
    ],
)
def test_unproven_rewrites_remain_unchanged(expression, suffix):
    query = f"SELECT a.id FROM readings a WHERE a.value>(SELECT {expression} FROM readings b WHERE b.bucket=a.bucket{suffix})"
    tree = qualify(parse_one(query))
    before = tree.sql()
    assert decorrelate_scalar_aggregate(tree).sql() == before


@pytest.mark.parametrize(
    "predicate",
    [
        "EXISTS(SELECT SUM(c.value) FROM readings c WHERE c.bucket=a.bucket AND c.value>999)",
        "EXISTS(SELECT COUNT(*) FROM readings c WHERE c.bucket=a.bucket)",
        "NOT EXISTS(SELECT SUM(c.value) FROM readings c WHERE c.bucket=a.bucket)",
        "EXISTS(SELECT c.value FROM readings c WHERE c.bucket=a.bucket AND c.value>5)",
    ],
)
@pytest.mark.parametrize("aggregate", ["AVG", "SUM", "MIN", "MAX"])
def test_scalar_rewrite_never_changes_a_sibling_query(predicate, aggregate):
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE readings(id INTEGER,bucket INTEGER,value REAL)")
        connection.executemany(
            "INSERT INTO readings VALUES(?,?,?)",
            [(1, 1, 2), (2, 1, 8), (3, 2, 30), (4, None, 5), (5, 3, None)],
        )
        query = (
            f"SELECT a.id, (SELECT {aggregate}(b.value) FROM readings b "
            f"WHERE b.bucket=a.bucket) AS x FROM readings a WHERE {predicate} ORDER BY a.id"
        )
        transformed = rewrite(query)
        assert (
            connection.execute(transformed.sql(dialect="sqlite")).fetchall()
            == connection.execute(query).fetchall()
        )


def test_rewrite_does_not_capture_an_existing_alias():
    query = """SELECT _sdd_aggregate_0.id,
        (SELECT AVG(b.value) FROM readings b WHERE b.bucket=_sdd_aggregate_0.bucket) AS x
        FROM readings AS _sdd_aggregate_0"""
    transformed = rewrite(query)
    assert transformed.args["joins"][0].this.alias != "_sdd_aggregate_0"
