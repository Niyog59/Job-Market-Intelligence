"""Duplicate handling: strong groups, possible-duplicate flags, review, and identity."""

import httpx
import pytest
from gh_fakes import DATA_DUTIES, FakeClock, ScriptedTransport, board, gh_job, ok, registry
from lever_fakes import lever_id, lever_job, lever_page

from jobmarket import cli, db
from jobmarket.availability import close_posting
from jobmarket.collector import Collector
from jobmarket.duplicates import (
    ReviewError,
    confirm_flag,
    dismiss_flag,
    dissolve_group,
    is_job_specific,
    normalize_url,
    run_rules,
)
from jobmarket.migrate import apply_migrations
from jobmarket.sources import sync_registry

pytestmark = pytest.mark.db

CLOSING = "<div>We are an equal opportunity employer.</div>"


@pytest.fixture
def denv(schema_conn, tmp_path):
    """Acme with two Greenhouse boards (acme, acme2) and a Lever board (lev)."""
    apply_migrations(schema_conn)
    sync_registry(
        schema_conn,
        registry(
            board("acme"),
            board("acme2", employer="Acme", slug="acme"),
            board("lev", employer="Acme", slug="acme", source="lever"),
        ),
        FakeClock()(),
    )

    class Env:
        conn = schema_conn
        clock = FakeClock()
        transport = ScriptedTransport()

        def collect(self, acme=(), acme2=(), lev=()):
            self.transport.queues["acme"] = [ok(list(acme))]
            self.transport.queues["acme2"] = [ok(list(acme2))]
            self.transport.queues["lev"] = [lever_page(list(lev))]
            client = httpx.Client(transport=self.transport)
            return Collector(
                self.conn, client, tmp_path / "raw", clock=self.clock, sleep=lambda s: None,
                interval=0,
            ).collect()  # fmt: skip

        def rows(self, sql, params=()):
            return self.conn.execute(sql, params).fetchall()

        def scalar(self, sql, params=()):
            return self.conn.execute(sql, params).fetchone()[0]

        def posting(self, token, source_id):
            return self.scalar(
                "SELECT posting_id FROM source_postings p JOIN source_boards b USING (board_id) "
                "WHERE b.board_key = %s AND p.source_posting_id = %s",
                (token, str(source_id)),
            )

    return Env()


def gh(job_id, token="acme", req=None, url=None, **kw):
    job = gh_job(job_id, token=token, **kw)
    job["requisition_id"] = req
    if url:
        job["absolute_url"] = url
    return job


def groups(env):
    return env.rows(
        "SELECT g.rule, g.status, array_agg(m.posting_id ORDER BY m.posting_id) "
        "FROM duplicate_groups g JOIN duplicate_group_members m USING (group_id) "
        "GROUP BY g.group_id ORDER BY g.group_id"
    )


def open_flags(env):
    return env.rows(
        "SELECT rule, posting_id, related_posting_id FROM posting_duplicate_flags "
        "WHERE status = 'open' ORDER BY flag_id"
    )


def test_same_employer_requisition_groups_records_across_boards(denv):
    report = denv.collect(acme=[gh(1, req="REQ-7")], acme2=[gh(50, token="acme2", req="req-7 ")])
    assert report.duplicate_groups_created == 1
    a, b = denv.posting("acme", 1), denv.posting("acme2", 50)
    assert groups(denv) == [("requisition_id", "active", sorted([a, b]))]
    again = denv.collect(acme=[gh(1, req="REQ-7")], acme2=[gh(50, token="acme2", req="REQ-7")])
    assert again.duplicate_groups_created == 0 and len(groups(denv)) == 1  # idempotent
    # Both source records stay separate, each with its own identity and history.
    assert denv.scalar("SELECT count(*) FROM source_postings") == 2


def test_requisition_reused_for_a_different_title_is_only_flagged(denv):
    denv.collect(
        acme=[gh(1, req="R9", title="Data Engineer")],
        acme2=[gh(50, token="acme2", req="R9", title="Senior Data Analyst")],
    )
    assert groups(denv) == []
    assert [f[0] for f in open_flags(denv)] == ["requisition_id_conflict"]


def test_identical_job_specific_application_url_groups_records(denv):
    url = "https://careers.acme.example/jobs?gh_jid=123456"
    denv.collect(acme=[gh(1, url=url)], acme2=[gh(50, token="acme2", url=url + "&gh_src=linkedin")])
    assert [g[0] for g in groups(denv)] == ["application_url"]


def test_a_shared_generic_careers_page_is_not_evidence(denv):
    url = "https://careers.acme.example/jobs"
    denv.collect(
        acme=[gh(1, url=url, title="Data Engineer")],
        acme2=[gh(50, token="acme2", url=url, title="Software Engineer")],
    )
    assert groups(denv) == [] and open_flags(denv) == []
    assert not is_job_specific(normalize_url(url), "1")


def lever_twin(n, **kw):
    """A Lever posting with the same visible description as gh_job's default content."""
    kw.setdefault("lists", [])
    return lever_job(n, **kw)


def test_same_title_place_and_text_across_sources_is_flagged_not_merged(denv):
    denv.collect(acme=[gh(1, content=DATA_DUTIES + CLOSING)], lev=[lever_twin(5)])
    a, b = denv.posting("acme", 1), denv.posting("lev", lever_id(5))
    assert open_flags(denv) == [("title_location_description", a, b)]
    assert groups(denv) == []  # source ids are not comparable; nothing is merged
    evidence = denv.scalar("SELECT evidence FROM posting_duplicate_flags")
    dates = {r["board"]: r["source_published_at"] for r in evidence["records"]}
    assert dates["lever:lev"] is None  # Lever gives no publication date; still matched
    assert dates["greenhouse:acme"] is not None


def test_multi_location_postings_match_on_any_shared_location(denv):
    denv.collect(
        acme=[gh(1, content=DATA_DUTIES + CLOSING, location="Dallas, TX")],
        lev=[lever_twin(5, location="Austin, TX", all_locations=["Austin, TX", "Dallas, TX"])],
    )
    assert len(open_flags(denv)) == 1


@pytest.mark.parametrize(
    ("second", "why"),
    [
        (dict(location="Houston, TX"), "no shared location"),
        (dict(content="<p>Own a different stack: Kafka, Spark, Scala.</p>"), "different text"),
        (dict(location=""), "missing location"),
    ],
)
def test_similarly_titled_distinct_openings_stay_unflagged(denv, second, why):
    if second.get("location") == "":
        job = gh(50, token="acme2")
        job["location"] = {"name": ""}
        job["offices"] = []
    else:
        job = gh(50, token="acme2", **second)
    denv.collect(acme=[gh(1, location="Dallas, TX")], acme2=[job])
    assert open_flags(denv) == [], why


def test_differing_requisitions_are_recorded_as_a_conflict_on_the_flag(denv):
    denv.collect(acme=[gh(1, req="A-1")], acme2=[gh(50, token="acme2", req="B-2")])
    (flag,) = open_flags(denv)
    assert flag[0] == "title_location_description"
    conflicts = denv.scalar("SELECT evidence -> 'conflicts' FROM posting_duplicate_flags")
    assert "requisition IDs differ" in conflicts[0]


def test_review_is_remembered_and_reversible(denv):
    jobs = dict(acme=[gh(1, content=DATA_DUTIES + CLOSING)], lev=[lever_twin(5)])
    denv.collect(**jobs)
    flag = denv.scalar("SELECT flag_id FROM posting_duplicate_flags")
    with pytest.raises(ReviewError):
        dismiss_flag(denv.conn, flag, " ", denv.clock())
    dismiss_flag(denv.conn, flag, "different teams", denv.clock())
    denv.collect(**jobs)
    assert open_flags(denv) == []  # a dismissed flag is not raised again

    denv.collect(acme=[gh(1, req="R1")], acme2=[gh(50, token="acme2", req="R1")])
    group = denv.scalar("SELECT group_id FROM duplicate_groups WHERE rule = 'requisition_id'")
    dissolve_group(denv.conn, group, "requisition numbers are reused per location", denv.clock())
    denv.collect(acme=[gh(1, req="R1")], acme2=[gh(50, token="acme2", req="R1")])
    assert [g[1] for g in groups(denv)] == ["dissolved"]  # not recreated


def test_confirming_a_flag_groups_the_records(denv):
    denv.collect(acme=[gh(1, content=DATA_DUTIES + CLOSING)], lev=[lever_twin(5)])
    flag = denv.scalar("SELECT flag_id FROM posting_duplicate_flags")
    group = confirm_flag(denv.conn, flag, "same opening on both boards", denv.clock())
    assert denv.scalar("SELECT confidence FROM duplicate_groups WHERE group_id = %s", (group,)) == (
        "reviewed"
    )
    assert open_flags(denv) == []


def test_closing_one_grouped_record_does_not_close_the_other(denv):
    denv.collect(acme=[gh(1, req="R1")], acme2=[gh(50, token="acme2", req="R1")])
    a, b = denv.posting("acme", 1), denv.posting("acme2", 50)
    close_posting(
        denv.conn, a, "employer page closed", "https://acme.example/1", "Closed.", denv.clock()
    )
    states = dict(denv.rows("SELECT posting_id, availability_state FROM source_postings"))
    assert (states[a], states[b]) == ("closed", "observed")


def test_rules_are_idempotent(denv):
    denv.collect(acme=[gh(1, content=DATA_DUTIES + CLOSING)], lev=[lever_twin(5)])
    second = run_rules(denv.conn, denv.clock())
    assert (second.groups_created, second.flags_added, second.members_added) == (0, 0, 0)


@pytest.fixture
def cli_env(denv, monkeypatch, isolated_schema):
    real_connect = db.connect

    def connect(settings, autocommit=False, options=None):
        return real_connect(
            settings, autocommit=autocommit, options=f"-c search_path={isolated_schema}"
        )

    monkeypatch.setattr(db, "connect", connect)
    return denv


def test_cli_duplicates_review(cli_env, capsys):
    cli_env.collect(acme=[gh(1, req="R1")], acme2=[gh(50, token="acme2", req="R1")])
    assert cli.main(["duplicates", "list"]) == cli.EXIT_OK
    assert "requisition_id (strong" in capsys.readouterr().out
    group = cli_env.scalar("SELECT group_id FROM duplicate_groups")
    assert cli.main(["duplicates", "dissolve", str(group), "--reason", "separate"]) == 0
    assert cli.main(["duplicates", "dissolve", str(group), "--reason", "again"]) == 1
    assert cli.main(["jobs"]) == cli.EXIT_OK
    assert "duplicate group" not in capsys.readouterr().out.split("dissolved")[-1]
