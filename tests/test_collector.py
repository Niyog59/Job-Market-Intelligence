"""End-to-end collection tests with a scripted HTTP transport and a controlled clock."""

import os
import smtplib
from datetime import UTC, datetime, timedelta

import httpx
import psycopg
import pytest
from gh_fakes import DATA_DUTIES, FakeClock, ScriptedTransport, board, gh_job, ok, registry

from jobmarket import db
from jobmarket.collector import COLLECTION_LOCK_KEY, CollectionBusy, Collector
from jobmarket.migrate import apply_migrations
from jobmarket.notify import SmtpSettings, send_pending
from jobmarket.raw_store import cleanup_raw
from jobmarket.sources import sync_registry

pytestmark = pytest.mark.db

HOUR = timedelta(hours=1)
BAD_JSON = httpx.Response(200, headers={"content-type": "application/json"}, content=b"{oops")
SERVER_ERROR = httpx.Response(503, content=b"unavailable")


@pytest.fixture
def env(schema_conn, tmp_path):
    apply_migrations(schema_conn)
    sync_registry(
        schema_conn, registry(board("acme"), board("beta", employer="Beta")), FakeClock()()
    )
    transport = ScriptedTransport()
    clock = FakeClock()

    class Env:
        conn = schema_conn
        raw_root = tmp_path / "raw"

        def __init__(self):
            self.transport, self.clock = transport, clock

        def collector(self):
            # A new Collector each time, as after a restart: all state is in the database.
            client = httpx.Client(transport=self.transport)
            return Collector(
                self.conn, client, self.raw_root, clock=self.clock, sleep=lambda s: None, interval=0
            )

        def one(self, sql, params=()):
            return self.conn.execute(sql, params).fetchone()

        def scalar(self, sql, params=()):
            return self.one(sql, params)[0]

    return Env()


def statuses(report):
    return {o.token: o.status for o in report.outcomes}


def test_successful_collection(env):
    env.transport.queue(
        "acme",
        ok(
            [
                gh_job(1),
                gh_job(2, title="Staff Accountant"),
                gh_job(3, location="London, United Kingdom"),
            ]
        ),
    )
    env.transport.queue("beta", ok([]))

    report = env.collector().collect()

    assert report.status == "complete"
    assert statuses(report) == {"acme": "complete", "beta": "complete"}
    acme = next(o for o in report.outcomes if o.token == "acme")
    assert (acme.reported, acme.received, acme.invalid) == (3, 3, 0)
    assert (acme.counts.retained, acme.counts.excluded) == (1, 2)
    # Every valid job is identified; only the retained one gets content history.
    assert env.scalar("SELECT count(*) FROM source_postings") == 3
    assert env.scalar("SELECT count(*) FROM posting_versions") == 1
    url, published = env.one(
        "SELECT v.absolute_url, o.source_published_at FROM posting_versions v "
        "JOIN posting_observations o USING (version_id)"
    )
    assert url == "https://job-boards.greenhouse.io/acme/jobs/1"
    assert published == datetime(2026, 10, 19, 14, 0, tzinfo=UTC)  # same instant as -05:00
    raw_files = list(env.raw_root.rglob("*.json"))
    assert len(raw_files) == 2
    assert env.scalar("SELECT count(*) FROM raw_responses") == 2
    assert env.scalar("SELECT count(*) FROM collection_incidents") == 0


def test_rerun_is_idempotent_and_records_only_genuine_changes(env):
    env.transport.queue(
        "acme",
        ok([gh_job(1)]),
        ok([gh_job(1, updated_at="2026-10-26T08:00:00-05:00")]),
        ok([gh_job(1, title="Senior Data Engineer")]),
    )
    env.transport.queue("beta", ok([]))
    for _ in range(3):
        env.collector().collect(tokens=["acme"])
        env.clock.advance(HOUR)

    assert env.scalar("SELECT count(*) FROM source_postings") == 1
    titles = [
        t for (t,) in env.conn.execute("SELECT title FROM posting_versions ORDER BY version_no")
    ]
    # The updated_at-only change did not create a version; the title change did.
    assert titles == ["Data Engineer", "Senior Data Engineer"]
    assert env.scalar("SELECT count(*) FROM posting_observations") == 3
    first, last = env.one("SELECT first_discovered_at, last_observed_at FROM source_postings")
    assert last - first >= 2 * HOUR


def test_failed_scan_keeps_records_and_never_counts_absence(env):
    env.transport.queue("acme", ok([gh_job(1), gh_job(2)]), BAD_JSON, ok([gh_job(1)], total=2))
    env.collector().collect(tokens=["acme"])
    before = env.conn.execute(
        "SELECT posting_id, last_observed_at, complete_scan_absences FROM source_postings "
        "ORDER BY posting_id"
    ).fetchall()

    env.clock.advance(HOUR)
    report = env.collector().collect(tokens=["acme"])
    assert statuses(report) == {"acme": "failed"}
    assert report.outcomes[0].error == "response is not valid JSON"
    env.clock.advance(HOUR)
    report = env.collector().collect(tokens=["acme"])  # job 2 missing, but scan is partial
    assert statuses(report) == {"acme": "partial"}

    after = env.conn.execute(
        "SELECT posting_id, last_observed_at, complete_scan_absences FROM source_postings "
        "ORDER BY posting_id"
    ).fetchall()
    assert after[1] == before[1]  # unseen job untouched by failed and partial scans
    assert after[0][2] == 0
    assert env.scalar("SELECT count(*) FROM source_postings") == 2


def test_complete_scan_counts_absence_without_closing(env):
    env.transport.queue("acme", ok([gh_job(1), gh_job(2)]), ok([gh_job(1)]))
    env.collector().collect(tokens=["acme"])
    env.collector().collect(tokens=["acme"])
    absences = dict(
        env.conn.execute("SELECT source_posting_id, complete_scan_absences FROM source_postings")
    )
    assert absences == {"1": 0, "2": 1}
    assert env.scalar("SELECT count(*) FROM retained_jobs") == 2  # nothing closed


def test_partial_response_keeps_valid_records_and_logs_rejects(env):
    broken = gh_job(2)
    broken["absolute_url"] = None
    env.transport.queue("acme", ok([gh_job(1), broken]))
    report = env.collector().collect(tokens=["acme"])
    assert statuses(report) == {"acme": "partial"}
    assert env.scalar("SELECT count(*) FROM source_postings") == 1
    reason, source_id = env.one("SELECT reason, source_posting_id FROM rejected_records")
    assert (reason, source_id) == ("missing or non-https absolute_url", "2")
    assert env.scalar("SELECT status FROM collection_incidents") == "retrying"


def test_one_board_failure_does_not_block_others(env):
    env.transport.queue("acme", httpx.ConnectError("connection refused"))
    env.transport.queue("beta", ok([gh_job(7, token="beta")]))
    report = env.collector().collect()
    assert statuses(report) == {"acme": "failed", "beta": "complete"}
    assert report.status == "partial"
    assert env.scalar("SELECT count(*) FROM raw_responses") == 1  # nothing to store for acme


def test_internal_error_on_one_board_is_contained(env, monkeypatch):
    import jobmarket.collector as collector_module

    real = collector_module.store_scan_results

    def explode(conn, board_id, *args, **kwargs):
        if (
            env.scalar("SELECT board_key FROM source_boards WHERE board_id = %s", (board_id,))
            == "acme"
        ):
            raise RuntimeError("disk on fire")
        return real(conn, board_id, *args, **kwargs)

    monkeypatch.setattr(collector_module, "store_scan_results", explode)
    env.transport.queue("acme", ok([gh_job(1)]))
    env.transport.queue("beta", ok([gh_job(7, token="beta")]))
    report = env.collector().collect()
    assert statuses(report) == {"acme": "failed", "beta": "complete"}
    assert "disk on fire" in report.outcomes[0].error
    assert env.scalar("SELECT count(*) FROM source_postings") == 1  # acme's rows rolled back


def test_timeouts_and_rate_limits(env):
    env.transport.queue("acme", httpx.ReadTimeout("slow"))
    env.transport.queue("beta", httpx.Response(429, headers={"retry-after": "7200"}))
    report = env.collector().collect()
    assert report.status == "failed"
    errors = {o.token: o.error for o in report.outcomes}
    assert errors == {"acme": "request timed out", "beta": "HTTP 429"}
    retries = dict(
        env.conn.execute(
            "SELECT b.board_key, i.next_retry_at - i.opened_at FROM collection_incidents i "
            "JOIN source_boards b USING (board_id)"
        )
    )
    assert retries["acme"] == HOUR
    assert retries["beta"] >= 2 * HOUR  # Retry-After of two hours is respected


def run_retry_cycle(env, token="acme"):
    """Initial failure, then three due retries one hour apart, all failing."""
    env.collector().collect(tokens=[token])
    for _ in range(3):
        env.clock.advance(HOUR)
        env.collector().retry_due()


def test_retries_follow_the_schedule_and_alert_once(env):
    env.transport.queue("acme", SERVER_ERROR)
    env.collector().collect(tokens=["acme"])
    opened, next_retry = env.one("SELECT opened_at, next_retry_at FROM collection_incidents")
    assert next_retry - opened == HOUR

    env.clock.advance(timedelta(minutes=30))
    report = env.collector().retry_due()
    assert report.run_id is None  # nothing due yet, no work done

    plan = []
    for expected_used in (1, 2, 3):
        env.clock.advance(timedelta(minutes=30) if expected_used == 1 else HOUR)
        report = env.collector().retry_due()
        assert [o.attempt_kind for o in report.outcomes] == ["retry"]
        used, nxt = env.one("SELECT retries_used, next_retry_at FROM collection_incidents")
        assert used == expected_used
        plan.append(None if nxt is None else nxt - opened)
    assert plan == [2 * HOUR, 3 * HOUR, None]

    status, alert_id = env.one("SELECT status, alert_notification_id FROM collection_incidents")
    assert status == "exhausted" and alert_id is not None
    kind, subject, body = env.one("SELECT kind, subject, body FROM notifications")
    assert kind == "alert" and "after 3 retries" in subject
    for expected in (
        "Acme (board 'acme')",
        "HTTP 503",
        "retries used: 3 of 3",
        "last complete collection: never",
        "nothing was closed or deleted",
    ):
        assert expected in body

    # Still failing later: no duplicate alert for the same incident.
    env.clock.advance(5 * HOUR)
    env.collector().collect(tokens=["acme"])
    assert env.scalar("SELECT count(*) FROM notifications") == 1

    # Recovery after an alert sends exactly one recovery notice.
    env.transport.queues["acme"] = [ok([gh_job(1)])]
    env.clock.advance(HOUR)
    report = env.collector().collect(tokens=["acme"])
    assert report.outcomes[0].incident_event == "resolved_after_alert"
    kinds = [k for (k,) in env.conn.execute("SELECT kind FROM notifications ORDER BY 1")]
    assert kinds == ["alert", "recovery"]
    assert env.scalar("SELECT status FROM collection_incidents") == "resolved"


def test_recovery_before_retries_run_out_sends_nothing(env):
    env.transport.queue("acme", SERVER_ERROR, ok([gh_job(1)]))
    env.collector().collect(tokens=["acme"])
    env.clock.advance(HOUR)
    report = env.collector().retry_due()
    assert report.outcomes[0].incident_event == "resolved"
    assert env.scalar("SELECT count(*) FROM notifications") == 0


def test_runs_inside_the_retry_window_do_not_use_or_reset_the_budget(env):
    env.transport.queue("acme", SERVER_ERROR)
    env.collector().collect(tokens=["acme"])
    env.clock.advance(timedelta(minutes=20))
    report = env.collector().collect(tokens=["acme"], trigger="scheduled")
    assert report.outcomes[0].attempt_kind == "extra"
    used, nxt, opened = env.one(
        "SELECT retries_used, next_retry_at, opened_at FROM collection_incidents"
    )
    assert (used, nxt - opened) == (0, HOUR)
    assert env.scalar("SELECT count(*) FROM collection_incidents") == 1


def test_overlapping_runs_are_refused(env, db_settings):
    with db.connect(db_settings, autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(%s)", (COLLECTION_LOCK_KEY,))
        with pytest.raises(CollectionBusy):
            env.collector().collect()
        other.execute("SELECT pg_advisory_unlock(%s)", (COLLECTION_LOCK_KEY,))
    assert env.scalar("SELECT count(*) FROM collection_runs") == 0


def test_abandoned_run_is_recovered_and_keeps_retry_state(env):
    env.transport.queue("acme", ok([gh_job(1)]))
    board_id = env.scalar("SELECT board_id FROM source_boards WHERE board_key = 'acme'")
    start = env.clock()
    run_id = env.scalar(
        "INSERT INTO collection_runs (trigger, started_at, status) "
        "VALUES ('manual', %s, 'running') RETURNING run_id",
        (start,),
    )
    env.conn.execute(
        "INSERT INTO board_scans (run_id, board_id, started_at, status) "
        "VALUES (%s, %s, %s, 'running')",
        (run_id, board_id, start),
    )

    report = env.collector().collect(tokens=["beta"])
    assert report.abandoned_scans_recovered == 1
    assert env.scalar("SELECT status FROM collection_runs WHERE run_id = %s", (run_id,)) == "failed"
    status, error = env.one(
        "SELECT status, error_message FROM board_scans WHERE run_id = %s", (run_id,)
    )
    assert status == "failed" and error.startswith("abandoned")
    assert env.scalar("SELECT status FROM collection_incidents") == "retrying"


def test_whole_run_failure_alerts_once_for_all_employers(env):
    env.transport.queue("acme", SERVER_ERROR)
    env.transport.queue("beta", httpx.ConnectError("network down"))
    env.collector().collect()
    for _ in range(3):
        env.clock.advance(HOUR)
        report = env.collector().retry_due()
    assert report.status == "failed"
    rows = env.conn.execute("SELECT subject, body FROM notifications").fetchall()
    assert len(rows) == 1
    subject, body = rows[0]
    assert "2 employer(s)" in subject
    assert "Acme" in body and "Beta" in body and "every board in this run failed" in body


def test_raw_cleanup_keeps_job_history(env):
    env.transport.queue("acme", ok([gh_job(1)]))
    env.transport.queue("beta", ok([]))
    env.collector().collect()
    orphan = env.raw_root / "greenhouse" / "acme" / "orphan.json"
    orphan.write_bytes(b"{}")
    old = (env.clock.now - timedelta(days=5)).timestamp()
    os.utime(orphan, (old, old))

    def history():
        return env.one(
            "SELECT (SELECT count(*) FROM source_postings), "
            "(SELECT count(*) FROM posting_versions), "
            "(SELECT count(*) FROM posting_observations), "
            "(SELECT count(*) FROM raw_responses)"
        )

    before = history()
    early = cleanup_raw(env.conn, env.raw_root, env.clock.now + timedelta(days=3))
    assert (early.files_deleted, early.orphan_files_deleted) == (0, 1)

    dry = cleanup_raw(env.conn, env.raw_root, env.clock.now + timedelta(days=5), dry_run=True)
    assert dry.files_deleted == 2 and len(list(env.raw_root.rglob("*.json"))) == 2

    result = cleanup_raw(env.conn, env.raw_root, env.clock.now + timedelta(days=5))
    assert (result.purged_rows, result.files_deleted) == (2, 2)
    assert list(env.raw_root.rglob("*.json")) == []
    assert env.scalar("SELECT count(*) FROM raw_responses WHERE purged_at IS NOT NULL") == 2
    assert history() == before  # job records, history, and raw metadata all kept


def test_possible_duplicates_are_flagged_not_merged(env):
    remote = {"location": "Remote", "offices": [{"name": "Distributed", "location": "Remote"}]}
    env.transport.queue(
        "acme",
        ok(
            [
                gh_job(
                    1, title="Software Engineer - Dallas, TX, USA", content=DATA_DUTIES, **remote
                ),
                gh_job(
                    2, title="Software Engineer - Houston, TX, USA", content=DATA_DUTIES, **remote
                ),
                gh_job(3, title="Data Engineer", content="<p>Different duties: SQL and dbt.</p>"),
            ]
        ),
    )
    report = env.collector().collect(tokens=["acme"])
    assert report.duplicate_flags_added == 1
    assert env.scalar("SELECT count(*) FROM retained_jobs") == 3
    assert env.scalar("SELECT count(*) FROM retained_jobs WHERE city_targeted_remote") == 2


def test_status_view_shows_failures_immediately(env):
    env.transport.queue("acme", SERVER_ERROR)
    env.transport.queue("beta", ok([gh_job(9, token="beta")]))
    env.collector().collect()
    rows = {
        r[0]: r[1:]
        for r in env.conn.execute(
            "SELECT token, last_scan_status, incident_status, last_complete_at IS NOT NULL, "
            "retained_postings FROM board_status"
        )
    }
    assert rows["acme"] == ("failed", "retrying", False, 0)
    assert rows["beta"] == ("complete", None, True, 1)


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, message):
        if self.host == "down.example":
            raise smtplib.SMTPServerDisconnected("gone")
        FakeSMTP.sent.append(message)


def test_outbox_delivery(env):
    env.transport.queue("acme", SERVER_ERROR)
    run_retry_cycle(env)
    assert send_pending(env.conn, None, env.clock.now).configured is False
    assert env.scalar("SELECT status FROM notifications") == "pending"

    def settings(host):
        return SmtpSettings(host, 587, "", "", "alerts@example.com", "me@example.com", True)

    failed = send_pending(env.conn, settings("down.example"), env.clock.now, FakeSMTP)
    assert (failed.sent, failed.failed) == (0, 1)
    assert env.one("SELECT status, attempts FROM notifications") == ("pending", 1)

    FakeSMTP.sent.clear()
    result = send_pending(env.conn, settings("smtp.example"), env.clock.now, FakeSMTP)
    assert (result.sent, result.failed) == (1, 0)
    assert FakeSMTP.sent[0]["To"] == "me@example.com"
    assert env.scalar("SELECT status FROM notifications") == "sent"


def test_disabled_board_cannot_be_collected(env):
    with pytest.raises(ValueError, match="unknown board"):
        env.collector().collect(tokens=["nope"])
    env.conn.execute("UPDATE source_boards SET is_active = false WHERE board_key = 'beta'")
    with pytest.raises(ValueError, match="disabled board"):
        env.collector().collect(tokens=["beta"])
    with pytest.raises(psycopg.errors.CheckViolation):
        env.conn.execute(
            "UPDATE source_boards SET is_active = true, "
            "verification_status = 'pending' WHERE board_key = 'beta'"
        )
