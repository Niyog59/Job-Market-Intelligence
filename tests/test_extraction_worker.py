"""The extraction worker: continuous work, idling, pauses and their reset, shutdown during a
retry, one worker at a time, the hourly batch stepping aside, and the reported metrics."""

from datetime import UTC, datetime, timedelta

import pytest
from extraction_fakes import ScriptedModel, quota_error, reply
from gh_fakes import T0, FakeClock, board, registry
from test_extraction import CONFIG, X, job
from test_extraction_lanes import teams
from test_history import HOUR, other_connection

from jobmarket import cli, db, extraction, extraction_worker
from jobmarket.extraction_worker import (
    IDLE_SECONDS,
    WORKER_LOCK_KEY,
    Stopper,
    latest_worker,
    run_unconfigured,
    run_worker,
    worker_running,
)
from jobmarket.gemini import ModelError
from jobmarket.migrate import apply_migrations
from jobmarket.sources import sync_registry

pytestmark = pytest.mark.db


@pytest.fixture
def x(schema_conn):
    apply_migrations(schema_conn)
    sync_registry(schema_conn, registry(board("acme")), FakeClock()())
    return X(schema_conn)


@pytest.fixture
def other(db_settings, isolated_schema):
    """A second process's connection to the same schema."""
    conn = other_connection(db_settings, isolated_schema)
    yield conn
    conn.close()


class FakeStopper(Stopper):
    """Waits move the test clock instead of sleeping. on_wait runs after every wait; the
    worker is stopped after stop_after waits (counting idle checks only when idle_only)."""

    def __init__(self, clock, stop_after=None, on_wait=None, idle_only=False):
        super().__init__()
        self.clock, self.stop_after, self.on_wait = clock, stop_after, on_wait
        self.idle_only = idle_only
        self.waits: list[float] = []

    def idle_waits(self) -> int:
        return self.waits.count(IDLE_SECONDS)

    def wait(self, seconds):
        self.waits.append(seconds)
        self.clock.advance(timedelta(seconds=max(0.0, seconds)))
        if self.on_wait:
            self.on_wait(self)
        counted = self.idle_waits() if self.idle_only else len(self.waits)
        if self.stop_after is not None and counted >= self.stop_after:
            self.stop()


def work(x, model, stopper, config=CONFIG, max_loops=200):
    lines = []
    report = run_worker(x.conn, model, config, x.clock, stopper, "worker@test", lines.append,
                        max_loops)  # fmt: skip
    return report, lines


def states(x):
    return x.conn.execute("SELECT state, detail FROM extraction_workers").fetchone()


def test_the_worker_extracts_new_listings_then_idles_until_stopped(x):
    x.collect(T0, [job(1), job(2)])

    def collect_more(stopper):  # a collection runs during the first idle check
        if stopper.waits[-1] == IDLE_SECONDS and stopper.idle_waits() == 1:
            x.collect(T0 + HOUR, [job(1), job(2), job(3)])

    stopper = FakeStopper(x.clock, stop_after=2, on_wait=collect_more, idle_only=True)
    model = ScriptedModel(reply())
    report, lines = work(x, model, stopper)
    assert teams(model) == [1, 2, 3]  # the new listing was picked up after one idle check
    assert stopper.idle_waits() == 2
    assert all(w > 0 for w in stopper.waits)  # every pass waits: never a busy loop
    assert report.succeeded == 3 and report.stop_reason == "stopped"
    assert states(x) == ("stopped", "stopped")
    assert "Idle: queue empty." in lines
    run = x.conn.execute("SELECT finished_at IS NOT NULL, succeeded FROM extraction_runs "
                         "WHERE run_id = %s", (report.run_id,)).fetchone()  # fmt: skip
    assert run == (True, 3)


def test_only_one_worker_runs_and_the_hourly_batch_steps_aside(x, other):
    other.execute("SELECT pg_advisory_lock(%s)", (WORKER_LOCK_KEY,))
    assert worker_running(x.conn)
    with pytest.raises(extraction.ExtractionBusy):
        work(x, ScriptedModel(reply()), FakeStopper(x.clock, stop_after=1))
    other.execute("SELECT pg_advisory_unlock(%s)", (WORKER_LOCK_KEY,))
    assert not worker_running(x.conn)


def test_a_daily_quota_error_pauses_the_worker_until_the_pacific_reset(x):
    x.collect(T0, [job(1)])
    stopper = FakeStopper(x.clock)
    seen = []

    def note(stopper):
        seen.append(states(x))
        if states(x)[0] == "working":
            stopper.stop()

    stopper.on_wait = note
    report, lines = work(x, ScriptedModel(quota_error(per_day=True), reply()), stopper)
    paused = [s for s in seen if s[0] == "paused"]
    assert paused and paused[0][1].startswith("daily quota exhausted; paused until")
    assert x.clock() >= datetime(2026, 10, 28, 7, 0, tzinfo=UTC)  # midnight Pacific passed
    assert report.succeeded == 1  # extracted after the reset, without a manual step
    assert max(stopper.waits) <= IDLE_SECONDS  # the pause sleeps in steps, with heartbeats


def test_stopping_during_a_retry_wait_puts_the_job_back(x):
    x.collect(T0, [job(1)])
    stopper = FakeStopper(x.clock, stop_after=1)  # the first wait is the 10 s retry wait
    model = ScriptedModel(ModelError("server_error", "503", 503), reply())
    report, _ = work(x, model, stopper)
    assert report.requests == 1 and stopper.waits == [10.0]
    status, kinds = x.conn.execute(
        "SELECT status, failure_kinds FROM extraction_jobs").fetchone()  # fmt: skip
    assert (status, kinds) == ("pending", [])  # back in the queue; an outage uses no try
    assert states(x) == ("stopped", "stopped")
    assert not worker_running(x.conn)


def test_the_worker_waits_for_a_retry_that_is_due_soon(x):
    x.collect(T0, [job(1)])
    stopper = FakeStopper(x.clock, stop_after=2)
    work(x, ScriptedModel(reply(text='{"role_categories": "oops"}'), reply()), stopper)
    assert stopper.waits[0] == pytest.approx(10.0, abs=0.1)  # the job's retry, not 15 s
    assert x.scalar("SELECT status FROM extraction_jobs") == "succeeded"


def test_a_rejected_request_pauses_for_an_hour(x):
    x.collect(T0, [job(1), job(2)])
    started = x.clock()
    stopper = FakeStopper(x.clock)
    stopper.on_wait = lambda s: s.stop() if x.clock() - started > HOUR / 2 else None
    work(x, ScriptedModel(ModelError("client_error", "API key not valid", 400)), stopper)
    state, detail = states(x)
    assert state == "stopped"
    logged = x.conn.execute("SELECT count(*) FROM extraction_attempts").fetchone()[0]
    assert logged == 1  # paused instead of trying the second listing with a bad key


def test_without_settings_the_worker_waits_and_says_why(x):
    stopper = FakeStopper(x.clock, stop_after=2)
    run_unconfigured(x.conn, "worker@test", x.clock, stopper, "GEMINI_API_KEY is not set")
    assert states(x) == ("stopped", "stopped while not configured")


def test_metrics_report_depth_age_and_latency_by_lane(x):
    x.collect(T0, [job(1)])
    x.track_from(T0 + HOUR / 2)
    x.collect(T0 + HOUR, [job(1), job(2)])
    x.clock.now = T0 + HOUR + timedelta(minutes=1)
    extraction.enqueue_new(x.conn, CONFIG, x.clock())
    x.clock.advance(timedelta(minutes=2))
    x.run(ScriptedModel(reply()), limit=1)  # the new listing, three minutes after collection
    info = extraction.queue_metrics(x.conn, CONFIG, x.clock())
    assert [(p, ready) for p, ready, _, _ in info["depth"]] == [(2, 1)]  # the backlog job
    new = next(t for t in info["timing"] if t.lane == "new")
    assert new.extracted == 1 and new.queue_p50 == pytest.approx(2.0, abs=0.1)
    assert new.collection_p50 == pytest.approx(3.0, abs=0.1)
    assert info["extracted_last_hour"] == 1
    assert dict(info["outcomes_today"]) == {"success": 1}


@pytest.fixture
def cli_db(x, monkeypatch, isolated_schema):
    real_connect = db.connect
    monkeypatch.setattr(
        db, "connect",
        lambda s, autocommit=False, options=None: real_connect(
            s, autocommit=autocommit, options=f"-c search_path={isolated_schema}"),
    )  # fmt: skip
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setenv(extraction.RPM_ENV, "60")
    monkeypatch.setenv(extraction.DAILY_ENV, "100")
    return x


def test_the_hourly_batch_skips_while_a_worker_runs(cli_db, other, capsys):
    other.execute("SELECT pg_advisory_lock(%s)", (WORKER_LOCK_KEY,))
    # Exit 3 marks the Airflow task skipped; no Gemini client is created.
    assert cli.main(["extract", "run", "--limit", "20", "--skip-if-worker"]) == cli.EXIT_BUSY
    assert "worker is running" in capsys.readouterr().out


def test_status_shows_the_worker_and_the_lanes(cli_db, capsys):
    cli_db.collect(T0, [job(1)])
    extraction.enqueue_new(cli_db.conn, CONFIG, cli_db.clock())
    extraction_worker.beat(cli_db.conn, "worker@test", cli_db.clock(), "paused",
                           "daily quota exhausted")  # fmt: skip
    assert latest_worker(cli_db.conn)[1] == "paused"
    assert cli.main(["extract", "status"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Worker worker@test: paused, process gone; daily quota exhausted" in out
    assert "Backlog lane: 0 of 0 used; 200 kept for new listings." in out
    assert "retained backlog" in out
