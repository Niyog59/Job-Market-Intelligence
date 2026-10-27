"""Greenhouse and Lever boards collected together: shared storage, history, completeness,
retries, alerts, and replay, end to end with a scripted HTTP transport."""

from datetime import timedelta

import httpx
import pytest
from gh_fakes import FakeClock, ScriptedTransport, board, gh_job, ok, registry
from lever_fakes import lever_id, lever_job, lever_page, lever_pages

from jobmarket import db
from jobmarket.collector import COLLECTION_LOCK_KEY, CollectionBusy, Collector, summarize
from jobmarket.migrate import apply_migrations
from jobmarket.replay import replay_scans
from jobmarket.sources import sync_registry

pytestmark = pytest.mark.db

HOUR = timedelta(hours=1)
UNAVAILABLE = httpx.Response(503, content=b"unavailable")


@pytest.fixture
def menv(schema_conn, tmp_path):
    """Greenhouse board acme and Lever boards lev and lev2, all enabled."""
    apply_migrations(schema_conn)
    reg = registry(
        board("acme"),
        board("lev", employer="Lev Co", source="lever"),
        board("lev2", employer="Lev Two", source="lever"),
    )
    sync_registry(schema_conn, reg, FakeClock()())

    class Env:
        conn = schema_conn
        raw_root = tmp_path / "raw"
        transport = ScriptedTransport()
        clock = FakeClock()

        def collector(self, workers=1):
            client = httpx.Client(transport=self.transport)
            return Collector(
                self.conn, client, self.raw_root, clock=self.clock, sleep=lambda s: None,
                interval=0, workers_per_source=workers,
            )  # fmt: skip

        def serve(self, token, *responses):
            """Replace what the board answers next. (queue() appends, and the transport
            repeats a board's last response, so leftovers from a previous run would be
            served first.)"""
            self.transport.queues[token] = list(responses)

        def scalar(self, sql, params=()):
            return self.conn.execute(sql, params).fetchone()[0]

        def rows(self, sql, params=()):
            return self.conn.execute(sql, params).fetchall()

    return Env()


def by_token(report):
    return {o.token: o for o in report.outcomes}


def lever_board_id(env, token):
    return env.scalar(
        "SELECT board_id FROM source_boards WHERE source_system = 'lever' AND board_key = %s",
        (token,),
    )


def test_both_sources_are_collected_into_the_same_history(menv):
    menv.serve("acme", ok([gh_job(1)]))
    menv.serve("lev", *lever_pages([lever_job(i) for i in range(1, 131)]))
    menv.serve("lev2", lever_page([lever_job(500, title="Staff Accountant")]))

    report = menv.collector().collect()

    out = by_token(report)
    assert report.status == "complete"
    assert {t: o.status for t, o in out.items()} == {
        "acme": "complete", "lev": "complete", "lev2": "complete",
    }  # fmt: skip
    assert (out["lev"].source_system, out["lev"].pages, out["lev"].received) == ("lever", 2, 130)
    assert out["lev"].counts.retained == 130 and out["lev2"].counts.excluded == 1
    # Raw pages: one file and row per page, numbered within the scan.
    pages = menv.rows(
        "SELECT r.sequence_no, r.storage_uri FROM raw_responses r JOIN board_scans s "
        "USING (board_scan_id) WHERE s.board_id = %s ORDER BY r.sequence_no",
        (lever_board_id(menv, "lev"),),
    )
    assert [p[0] for p in pages] == [1, 2]
    assert pages[0][1].startswith("lever/lev/") and pages[1][1].endswith("-p2.json")
    assert all((menv.raw_root / uri).exists() for _, uri in pages)
    # Every observation points at the page its posting came from.
    page_of = dict(
        menv.rows(
            "SELECT p.source_posting_id, r.sequence_no FROM posting_observations o "
            "JOIN source_postings p USING (posting_id) "
            "JOIN raw_responses r ON r.raw_response_id = o.raw_response_id"
        )
    )
    assert page_of[lever_id(1)] == 1 and page_of[lever_id(130)] == 2
    # Lever fields are kept; createdAt is stored as creation time, never as publication.
    url, apply_url, created, published, source = menv.conn.execute(
        "SELECT absolute_url, apply_url, source_created_at, source_published_at, source_system "
        "FROM retained_jobs WHERE source_posting_id = %s",
        (lever_id(1),),
    ).fetchone()
    assert url.endswith(lever_id(1)) and apply_url.endswith("/apply")
    assert created is not None and published is None and source == "lever"
    assert (
        menv.scalar(
            "SELECT source_fields -> 'categories' ->> 'location' FROM posting_versions v "
            "JOIN source_postings p USING (posting_id) WHERE p.source_posting_id = %s",
            (lever_id(1),),
        )
        == "Dallas, TX"
    )
    assert any("lever" in line for line in summarize(report))


def test_same_job_id_on_two_sources_is_two_postings(menv):
    # Identity is source system + board + source id; a matching title never merges jobs.
    menv.serve("acme", ok([gh_job(7, title="Data Engineer")]))
    menv.serve("lev", lever_page([lever_job(7, title="Data Engineer")]))
    menv.serve("lev2", lever_page([lever_job(7, title="Data Engineer", token="lev2")]))
    menv.collector().collect()
    assert menv.scalar("SELECT count(*) FROM source_postings") == 3


def test_a_failing_lever_page_gives_a_partial_scan_and_never_counts_absences(menv):
    first = [lever_job(i) for i in range(1, 101)]
    menv.serve("acme", ok([gh_job(1)]))
    menv.serve("lev2", lever_page([]))
    menv.serve("lev", *lever_pages(first + [lever_job(150)]))
    menv.collector().collect()
    menv.clock.advance(HOUR)
    # Second run: page 1 arrives, page 2 fails. Posting 150 was on page 2 and is unseen.
    menv.serve("lev", lever_page(first), UNAVAILABLE)

    report = menv.collector().collect()

    out = by_token(report)
    assert out["lev"].status == "partial" and "page 2 unusable (HTTP 503)" in out["lev"].error
    assert out["acme"].status == "complete" and out["lev2"].status == "complete"
    assert report.status == "partial"
    assert menv.scalar(
        "SELECT complete_scan_absences FROM source_postings WHERE source_posting_id = %s",
        (lever_id(150),),
    ) == 0  # fmt: skip
    assert menv.scalar(
        "SELECT kind FROM collection_incidents WHERE board_id = %s", (lever_board_id(menv, "lev"),)
    ) == "partial"  # fmt: skip
    # The records that did arrive are kept as observations of this scan.
    assert out["lev"].counts.unchanged == 100


def test_board_failures_are_isolated(menv):
    menv.serve("acme", ok([gh_job(1)]))
    menv.serve("lev", httpx.ConnectError("network down"))
    menv.serve("lev2", httpx.Response(404, json={"ok": False}))
    report = menv.collector().collect()
    out = by_token(report)
    assert out["acme"].status == "complete"
    assert out["lev"].status == "failed" and "request failed" in out["lev"].error
    assert out["lev2"].status == "failed" and "board not found" in out["lev2"].error
    assert report.status == "partial"
    assert menv.scalar("SELECT count(*) FROM collection_incidents") == 2


def test_retries_survive_restarts_run_only_failed_boards_and_alert_once(menv):
    menv.serve("acme", ok([gh_job(1)]))
    menv.serve("lev2", lever_page([]))
    menv.serve("lev", UNAVAILABLE)
    menv.collector().collect()
    lev = lever_board_id(menv, "lev")

    for _ in range(3):
        menv.clock.advance(HOUR)
        before = len(menv.transport.requests)
        # A new Collector each time, as after a restart: retry state is in the database.
        report = menv.collector().retry_due()
        assert [o.token for o in report.outcomes] == ["lev"]  # successful boards are not rerun
        assert {r.url.path.split("/")[3] for r in menv.transport.requests[before:]} == {"lev"}

    status, retries, alert = menv.conn.execute(
        "SELECT status, retries_used, alert_notification_id FROM collection_incidents "
        "WHERE board_id = %s",
        (lev,),
    ).fetchone()
    assert (status, retries) == ("exhausted", 3) and alert is not None
    assert menv.scalar("SELECT count(*) FROM notifications WHERE kind = 'alert'") == 1
    assert menv.collector().retry_due().run_id is None  # nothing more is due

    menv.clock.advance(HOUR)
    menv.serve("lev", lever_page([lever_job(1)]))
    menv.serve("acme", ok([gh_job(1)]))
    menv.collector().collect()
    assert menv.scalar("SELECT count(*) FROM notifications WHERE kind = 'recovery'") == 1
    assert menv.scalar("SELECT count(*) FROM notifications WHERE kind = 'alert'") == 1


def test_status_view_reports_attempts_and_due_retries(menv):
    menv.serve("acme", ok([gh_job(1)]))
    menv.serve("lev2", lever_page([]))
    menv.serve("lev", UNAVAILABLE)
    menv.collector().collect()
    row = menv.conn.execute(
        "SELECT source_system, last_scan_status, last_scan_pages, incident_status, "
        "incident_attempts, retry_due, last_complete_at FROM board_status WHERE token = 'lev'"
    ).fetchone()
    # The retry is scheduled an hour after the test clock, which is in the past for now().
    assert row == ("lever", "failed", 1, "retrying", 1, True, None)


def test_a_second_command_cannot_claim_the_same_retries(menv, db_settings, isolated_schema):
    with db.connect(
        db_settings, autocommit=True, options=f"-c search_path={isolated_schema}"
    ) as other:
        other.execute("SELECT pg_advisory_lock(%s)", (COLLECTION_LOCK_KEY,))
        with pytest.raises(CollectionBusy):
            menv.collector().retry_due()
        other.execute("SELECT pg_advisory_unlock(%s)", (COLLECTION_LOCK_KEY,))


def test_replaying_a_multi_page_scan_adds_nothing(menv):
    menv.serve("acme", ok([]))
    menv.serve("lev2", lever_page([]))
    menv.serve("lev", *lever_pages([lever_job(i) for i in range(1, 151)]))
    menv.collector().collect()
    scan = menv.scalar(
        "SELECT max(board_scan_id) FROM board_scans WHERE board_id = %s",
        (lever_board_id(menv, "lev"),),
    )
    facts = (
        "SELECT (SELECT count(*) FROM posting_observations), "
        "(SELECT count(*) FROM posting_versions)"
    )
    before = menv.conn.execute(facts).fetchone()

    (result,) = replay_scans(menv.conn, menv.raw_root, [scan], menv.clock)

    assert (result.outcome, result.validation) == ("processed", "complete")
    assert result.counts.already_recorded == 150
    assert menv.conn.execute(facts).fetchone() == before


def test_replaying_a_scan_that_stopped_early_is_partial_again(menv):
    menv.serve("acme", ok([]))
    menv.serve("lev2", lever_page([]))
    menv.serve("lev", lever_page([lever_job(i) for i in range(1, 101)]), httpx.ReadTimeout("slow"))
    report = menv.collector().collect()
    assert by_token(report)["lev"].status == "partial"
    scan = by_token(report)["lev"].board_scan_id
    (result,) = replay_scans(menv.conn, menv.raw_root, [scan], menv.clock)
    assert (result.outcome, result.validation) == ("processed", "partial")


def test_lever_history_follows_the_shared_change_rules(menv):
    def collect(job):
        menv.serve("acme", ok([]))
        menv.serve("lev2", lever_page([]))
        menv.serve("lev", lever_page([job]))
        menv.collector().collect()
        menv.clock.advance(HOUR)

    collect(lever_job(1))
    collect(lever_job(1, created_at=1_793_100_000_000))  # only createdAt differs
    collect(lever_job(1, description="<div><p>Build and maintain data pipelines with SQL, "
                      "Python, and Airflow.  Model data in Snowflake and publish dashboards in "
                      "Tableau.</p></div>"))  # fmt: skip
    collect(lever_job(1, workplace="remote"))  # a meaningful change
    collect(lever_job(1))  # and back again: A, B, A

    stages = menv.rows(
        "SELECT s.version_no, s.changed_fields FROM posting_version_stages s "
        "JOIN source_postings p USING (posting_id) WHERE p.source_posting_id = %s "
        "ORDER BY s.stage_no",
        (lever_id(1),),
    )
    assert [s[0] for s in stages] == [1, 2, 3]
    assert stages[1][1] == ["locations"]
    assert menv.scalar("SELECT count(*) FROM posting_observations") == 5
    assert menv.rows("SELECT * FROM posting_history_problems") == []


def test_parallel_fetch_lanes_keep_results_in_order(menv):
    menv.serve("acme", ok([gh_job(1)]))
    menv.serve("lev", *lever_pages([lever_job(i) for i in range(1, 251)]))
    menv.serve("lev2", *lever_pages([lever_job(i) for i in range(301, 421)]))

    report = menv.collector(workers=2).collect()

    assert [o.token for o in report.outcomes] == ["acme", "lev", "lev2"]
    assert all(o.status == "complete" for o in report.outcomes)
    assert menv.scalar("SELECT count(*) FROM source_postings") == 1 + 250 + 120
    scan_ids = [o.board_scan_id for o in report.outcomes]
    assert scan_ids == sorted(scan_ids)
