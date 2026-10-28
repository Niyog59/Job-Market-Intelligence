"""Availability states, qualifying absences, and board holds, end to end."""

from datetime import timedelta

import httpx
import pytest
from gh_fakes import gh_job, ok

from jobmarket import availability, cli, db
from jobmarket.availability import AvailabilityError, HoldThresholds, close_posting, release_hold
from jobmarket.connectors import Pacer
from jobmarket.evidence import check_pages, page_targets
from jobmarket.greenhouse import GreenhouseConnector
from jobmarket.replay import replay_scans

pytestmark = pytest.mark.db

HOUR = timedelta(hours=1)
FAILED = httpx.Response(503, content=b"unavailable")


class Board:
    """Drive the acme board through scans and read back availability."""

    def __init__(self, env):
        self.env = env

    def scan(self, ids, response=None):
        self.env.transport.queues["acme"] = [response or ok([gh_job(i) for i in ids])]
        self.env.transport.queues["beta"] = [ok([])]
        report = self.env.collector().collect(tokens=["acme"])
        self.env.clock.advance(HOUR)
        return report.outcomes[0]

    def state(self, source_id):
        return self.env.one(
            "SELECT availability_state, qualifying_absences FROM source_postings p "
            "JOIN source_boards b USING (board_id) WHERE b.board_key = 'acme' "
            "AND p.source_posting_id = %s",
            (str(source_id),),
        )

    def posting_id(self, source_id):
        return self.env.scalar(
            "SELECT posting_id FROM source_postings WHERE source_posting_id = %s",
            (str(source_id),),
        )

    def last_check(self):
        return self.env.one(
            "SELECT outcome, reference_count, missing_count, hold_id FROM absence_checks "
            "ORDER BY board_scan_id DESC LIMIT 1"
        )

    def active_ids(self):
        return {r[0] for r in self.env.conn.execute("SELECT source_posting_id FROM active_jobs")}


@pytest.fixture
def acme(env):
    return Board(env)


def test_third_qualifying_absence_hides_the_job(acme):
    assert acme.scan([1, 2, 3]).availability.outcome == "baseline"
    assert acme.scan([1, 2, 3]).availability.outcome == "qualifying"
    acme.scan([1, 2])
    assert acme.state(3) == ("possibly_unavailable", 1)
    assert acme.active_ids() == {"1", "2", "3"}  # still shown, with a warning
    acme.scan([1, 2])
    assert acme.state(3) == ("possibly_unavailable", 2)
    acme.scan([1, 2])
    assert acme.state(3) == ("assumed_inactive", 3)
    assert acme.active_ids() == {"1", "2"}
    assert acme.env.scalar("SELECT count(*) FROM retained_jobs") == 3  # hidden, not deleted
    reasons = [
        r[0]
        for r in acme.env.conn.execute(
            "SELECT reason FROM posting_availability_events WHERE posting_id = %s "
            "ORDER BY event_id",
            (acme.posting_id(3),),
        )
    ]
    assert reasons == [
        "first observed",
        "missing from qualifying scan 1 of 3",
        "missing from qualifying scan 2 of 3",
        "missing from qualifying scan 3 of 3",
    ]


def test_failed_and_partial_scans_pause_counting(acme):
    acme.scan([1, 2, 3])
    acme.scan([1, 2])
    assert acme.state(3) == ("possibly_unavailable", 1)
    assert acme.scan([], FAILED).availability.outcome == "not_complete"
    partial = acme.scan([], ok([gh_job(1), gh_job(2)], total=5))  # count mismatch
    assert partial.status == "partial" and partial.availability.outcome == "not_complete"
    assert acme.state(3) == ("possibly_unavailable", 1)  # neither advanced nor reset
    acme.scan([1, 2])
    assert acme.state(3) == ("possibly_unavailable", 2)


def test_replaying_a_scan_never_counts_again(acme):
    acme.scan([1, 2, 3])
    last = acme.scan([1, 2]).board_scan_id
    checks = acme.env.scalar("SELECT count(*) FROM absence_checks")
    (result,) = replay_scans(acme.env.conn, acme.env.raw_root, [last], acme.env.clock)
    assert result.outcome == "processed"
    assert acme.state(3) == ("possibly_unavailable", 1)
    assert acme.env.scalar("SELECT count(*) FROM absence_checks") == checks


def test_scope_change_starts_a_new_baseline(acme, monkeypatch):
    acme.scan([1, 2, 3])
    acme.scan([1, 2, 3])
    monkeypatch.setattr(GreenhouseConnector, "scope_key", lambda self, region: "greenhouse:v2")
    changed = acme.scan([1, 2])
    assert changed.availability.outcome == "scope_changed"
    assert acme.state(3) == ("observed", 0)  # the changeover scan counts nothing
    assert acme.scan([1, 2]).availability.outcome == "qualifying"
    assert acme.state(3) == ("possibly_unavailable", 1)


def test_reappearance_restores_observed_and_keeps_history(acme):
    acme.scan([1, 2, 3])
    first = acme.env.scalar(
        "SELECT first_discovered_at FROM source_postings WHERE source_posting_id = '3'"
    )
    for _ in range(4):
        acme.scan([1, 2])
    assert acme.state(3) == ("assumed_inactive", 3)
    back = acme.scan([1, 2, 3])
    assert back.availability.reappeared == 1
    assert acme.state(3) == ("observed", 0)
    assert acme.env.scalar(
        "SELECT first_discovered_at FROM source_postings WHERE source_posting_id = '3'"
    ) == first  # fmt: skip
    assert acme.env.scalar(
        "SELECT count(*) FROM posting_availability_events WHERE posting_id = %s",
        (acme.posting_id(3),),
    ) == 5  # first observed, three absences, listed again  # fmt: skip
    acme.scan([1, 2])
    assert acme.state(3) == ("possibly_unavailable", 1)  # counting restarts from zero


def test_partial_scan_still_records_a_reappearance(acme):
    acme.scan([1, 2, 3])
    acme.scan([1, 2])
    acme.scan([], ok([gh_job(1), gh_job(3)], total=5))
    assert acme.state(3) == ("observed", 0)


def test_manual_closure_hides_at_once_and_needs_evidence(acme):
    acme.scan([1, 2])
    pid = acme.posting_id(2)
    with pytest.raises(AvailabilityError):
        close_posting(acme.env.conn, pid, "closed", "", "", acme.env.clock())
    close_posting(
        acme.env.conn, pid, "employer page says the role is closed",
        "https://acme.example/jobs/2", "This position is closed.", acme.env.clock(),
    )  # fmt: skip
    assert acme.state(2) == ("closed", 0)
    assert acme.active_ids() == {"1"}
    acme.scan([1, 2])  # listed again after the closure
    assert acme.state(2) == ("closed", 0)
    assert acme.env.scalar(
        "SELECT reason FROM posting_availability_events WHERE posting_id = %s "
        "ORDER BY event_id DESC LIMIT 1",
        (pid,),
    ).startswith("listed again after an official closure")


def test_page_text_closure_only_on_the_postings_own_page(acme):
    acme.scan([1, 2, 3])
    pages = {
        "/acme/jobs/1": httpx.Response(
            200, text="<h1>Data Engineer</h1><p>We are no longer accepting applications.</p>"
        ),
        "/acme/jobs/2": httpx.Response(404, text="Not found"),
        "/acme/jobs/3": httpx.Response(
            302, headers={"location": "https://job-boards.greenhouse.io/acme"}
        ),
        "/acme": httpx.Response(200, text="This position has been filled."),
    }
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: pages[r.url.path]), follow_redirects=True
    )
    targets = page_targets(acme.env.conn, per_board=5)
    results = {r.target.source_posting_id: r.outcome for r in check_pages(
        acme.env.conn, client, targets, acme.env.clock, Pacer(0))}  # fmt: skip
    assert results == {"1": "closed_notice", "2": "not_found", "3": "redirected"}
    assert acme.state(1) == ("closed", 0)
    assert acme.state(2) == ("observed", 0)  # a missing page is not proof of closure
    assert acme.state(3) == ("observed", 0)  # a board front page is not the job's page
    evidence = acme.env.scalar(
        "SELECT evidence FROM posting_availability_events WHERE to_state = 'closed'"
    )
    assert evidence["source"] == "page check" and "no longer accepting" in evidence["text"]


def test_all_jobs_missing_opens_a_hold_even_on_a_small_board(acme):
    acme.scan([1, 2])
    held = acme.scan([])
    assert held.availability.outcome == "mass_disappearance"
    assert acme.last_check()[:3] == ("mass_disappearance", 2, 2)
    assert acme.state(1) == ("observed", 0) and acme.state(2) == ("observed", 0)
    again = acme.scan([])  # the same suspicious result does not release the hold
    assert again.availability.outcome == "held"
    assert acme.env.scalar("SELECT count(*) FROM board_holds WHERE status = 'active'") == 1
    acme.scan([9])  # new jobs are still saved during a hold
    assert acme.state(9) == ("observed", 0)
    assert acme.state(1) == ("observed", 0)


@pytest.mark.parametrize(
    ("reference", "present", "outcome"),
    [
        (range(1, 21), range(11, 21), "mass_disappearance"),  # 10 of 20 missing: 50% and 10
        (range(1, 19), range(10, 19), "qualifying"),  # 9 of 18: 50% but fewer than 10
        (range(1, 26), range(11, 26), "qualifying"),  # 10 of 25: only 40%
    ],
)
def test_share_threshold(acme, reference, present, outcome):
    acme.scan(list(reference))
    assert acme.scan(list(present)).availability.outcome == outcome


def test_changed_ids_with_an_unchanged_total_are_detected(acme):
    acme.scan(range(1, 11))
    swapped = acme.scan(range(11, 21))  # still 10 jobs, all different
    assert swapped.availability.outcome == "mass_disappearance"
    assert acme.last_check()[:3] == ("mass_disappearance", 10, 10)
    assert acme.state(1) == ("observed", 0)


def test_thresholds_are_configurable(acme, monkeypatch):
    monkeypatch.setenv(availability.HOLD_MIN_MISSING_ENV, "2")
    acme.scan([1, 2, 3, 4])
    assert acme.scan([1, 2]).availability.outcome == "mass_disappearance"


def test_release_after_collection_fix_keeps_the_trusted_reference(acme):
    acme.scan(range(1, 11))
    acme.scan([])
    acme.scan([])
    (hold_id,) = acme.env.one("SELECT hold_id FROM board_holds")
    with pytest.raises(AvailabilityError):
        release_hold(acme.env.conn, hold_id, "collection_issue_resolved", " ", acme.env.clock())
    release_hold(
        acme.env.conn, hold_id, "collection_issue_resolved",
        "token typo fixed in the registry", acme.env.clock(),
    )  # fmt: skip
    after = acme.scan(range(1, 10))  # job 10 really gone
    assert after.availability.outcome == "qualifying"
    assert acme.state(10) == ("possibly_unavailable", 1)  # held scans were never counted
    assert acme.state(1) == ("observed", 0)
    with pytest.raises(AvailabilityError, match="already released"):
        release_hold(acme.env.conn, hold_id, "collection_issue_resolved", "x", acme.env.clock())


def test_release_after_verified_removals_restarts_counting(acme):
    acme.scan(range(1, 13))
    acme.scan([1])  # 11 of 12 missing
    (hold_id,) = acme.env.one("SELECT hold_id FROM board_holds")
    new_reference = release_hold(
        acme.env.conn, hold_id, "source_removals_verified",
        "employer careers page lists one opening", acme.env.clock(),
    )  # fmt: skip
    assert new_reference is not None
    for expected in (1, 2, 3):
        result = acme.scan([1])
        assert result.availability.outcome == "qualifying"  # not held again
        assert acme.state(5)[1] == expected
    assert acme.state(5) == ("assumed_inactive", 3)
    assert acme.active_ids() == {"1"}


def test_explicit_closure_still_works_during_a_hold(acme):
    acme.scan([1, 2])
    acme.scan([])
    close_posting(
        acme.env.conn, acme.posting_id(1), "employer email", "https://acme.example/x",
        "Applications are closed.", acme.env.clock(),
    )  # fmt: skip
    assert acme.state(1) == ("closed", 0)


def test_judging_error_rolls_back_the_whole_board(acme, monkeypatch):
    acme.scan([1])
    real = availability.judge_scan
    calls = []

    def flaky(conn, board_id, scan_id, outcome, *args, **kwargs):
        calls.append(outcome)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return real(conn, board_id, scan_id, outcome, *args, **kwargs)

    monkeypatch.setattr(availability, "judge_scan", flaky)
    result = acme.scan([1, 2])
    assert result.status == "failed" and "boom" in result.error
    assert (
        acme.env.scalar("SELECT count(*) FROM source_postings WHERE source_posting_id = '2'") == 0
    )
    assert acme.last_check()[0] == "not_complete"


def test_hold_thresholds_from_environment(monkeypatch):
    monkeypatch.setenv(availability.HOLD_ALL_MISSING_ENV, "false")
    monkeypatch.setenv(availability.HOLD_MIN_SHARE_ENV, "0.8")
    t = HoldThresholds.from_env()
    assert (t.all_missing, t.min_share, t.min_missing) == (False, 0.8, 10)
    assert t.trigger(2, 2) is None and t.trigger(10, 10) == "share_missing"


def test_status_view_reports_holds_and_trusted_checks(acme):
    acme.scan([1, 2])
    acme.scan([])
    row = acme.env.one(
        "SELECT hold_id IS NOT NULL, hold_trigger, last_trusted_check_at IS NOT NULL "
        "FROM board_status WHERE token = 'acme'"
    )
    assert row == (True, "all_missing", True)


@pytest.fixture
def cli_env(env, monkeypatch, isolated_schema):
    real_connect = db.connect

    def connect(settings, autocommit=False, options=None):
        return real_connect(
            settings, autocommit=autocommit, options=f"-c search_path={isolated_schema}"
        )

    monkeypatch.setattr(db, "connect", connect)
    return env


def test_cli_review_commands(cli_env, capsys):
    acme = Board(cli_env)
    acme.scan([1, 2, 3])
    acme.scan([1, 2])
    assert cli.main(["jobs"]) == cli.EXIT_OK
    assert "WARNING: possibly unavailable" in capsys.readouterr().out
    assert cli.main(["availability", "show", str(acme.posting_id(3))]) == cli.EXIT_OK
    assert "missing from qualifying scan 1 of 3" in capsys.readouterr().out
    acme.scan([])
    assert cli.main(["availability", "holds"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "2 of 2 reference jobs missing (all_missing)" in out
    hold = cli_env.scalar("SELECT hold_id FROM board_holds")
    release = ["availability", "release", str(hold), "--reason", "collection-issue-resolved"]
    assert cli.main([*release, "--evidence", "network fixed"]) == cli.EXIT_OK
    assert "released" in capsys.readouterr().out
    close = ["availability", "close", str(acme.posting_id(1)), "--reason", "r"]
    assert cli.main([*close, "--evidence-url", "https://x.example", "--evidence-text", "x"]) == 0
    assert cli.main(["jobs"]) == cli.EXIT_OK
    assert "assumed inactive or closed; use --all-states" in capsys.readouterr().out


def test_closure_wording_inside_scripts_is_ignored():
    from jobmarket.evidence import closure_notice

    page = (
        "<script>var i18n={closed:'This job is no longer accepting applications'}</script>"
        "<h1>Data Engineer</h1><p>Apply below.</p>"
    )
    assert closure_notice(page) is None
    assert "has been filled" in closure_notice("<p>This position has been filled.</p>")
