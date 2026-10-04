"""Result publication budgets are independent of semantic completion."""

import pytest

from test_query_jobs import source as source, submit
from sdd.generic.query_jobs import QueryJobs
from sdd.generic.results import RESULT_BYTES


@pytest.mark.parametrize("oversized_field", ["result", "metadata"])
def test_job_rejects_oversized_rows_or_metadata_before_publication(source, oversized_field):
    db, _ = source
    saved = submit(db)
    jobs = QueryJobs(db)
    claim = jobs.claim("team")
    response = {"manifest": {"complete": True}, "result": []}
    response[oversized_field] = ["x" * RESULT_BYTES]
    assert jobs.finish(claim, response)
    final = jobs.get("team", "alice", saved["id"])
    assert final["job_state"] == "FAILED" and final["error"] == "RESULT_TOO_LARGE"
    assert final["output_state"] == "NOT_EVALUATED" and final["result"] is None
