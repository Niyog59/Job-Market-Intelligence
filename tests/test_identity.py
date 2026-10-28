"""Identity of source records without a usable source id."""

import pytest
from gh_fakes import gh_job, ok

pytestmark = pytest.mark.db


def test_missing_source_id_uses_the_job_url_and_is_marked(env):
    no_id = gh_job(777)
    no_id.pop("id")
    nothing = gh_job(778)
    nothing.pop("id")
    nothing["absolute_url"] = "https://job-boards.greenhouse.io/acme"
    env.transport.queues["acme"] = [ok([no_id, nothing])]
    (acme,) = env.collector().collect(tokens=["acme"]).outcomes
    assert acme.status == "partial"  # the record without any identity is not silently dropped
    assert env.conn.execute(
        "SELECT source_posting_id, identity_basis FROM source_postings"
    ).fetchall() == [("777", "job_url")]
    reason, detail = env.one("SELECT reason, detail FROM rejected_records")
    assert "identity unknown" in reason and detail["absolute_url"].endswith("/acme")
    env.transport.queues["acme"] = [ok([gh_job(777)])]
    env.collector().collect(tokens=["acme"])  # the same job later arrives with its id
    assert env.one("SELECT source_posting_id, identity_basis FROM source_postings") == (
        "777",
        "source_id",
    )


def test_greenhouse_ids_in_job_urls():
    from jobmarket.greenhouse import id_from_url

    assert id_from_url("https://job-boards.greenhouse.io/acme/jobs/4433145009") == 4433145009
    assert id_from_url("https://careers.acme.example/open?gh_jid=123456") == 123456
    assert id_from_url("https://job-boards.greenhouse.io/acme") is None
