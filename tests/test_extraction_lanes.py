"""Extraction lanes, claims shared between processes, crash recovery, and retries that do not
hold up other listings. The model is always a scripted fake."""

import re
from datetime import timedelta

import pytest
from extraction_fakes import ScriptedModel, reply
from gh_fakes import T0, FakeClock, board, ok, registry
from test_extraction import CONFIG, X, job
from test_history import HOUR, other_connection

from jobmarket import extraction
from jobmarket.extraction import ExtractionConfig, enqueue_new
from jobmarket.migrate import apply_migrations
from jobmarket.sources import sync_registry

pytestmark = pytest.mark.db

INVALID = '{"role_categories": "oops"}'


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


def accountant(n):
    return job(n, title="Staff Accountant", content=f"<p>Close the books monthly, team {n}.</p>")


def teams(model):
    """The team number of each request, in the order the requests were sent."""
    return [int(re.search(r"team (\d+)", text).group(1)) for text in model.calls]


def with_lanes(x, backlog, new):
    """Collect `backlog` before extraction tracking started and `new` after it."""
    x.collect(T0, backlog)
    x.track_from(T0 + HOUR / 2)
    x.collect(T0 + HOUR, backlog + new)
    enqueue_new(x.conn, CONFIG, x.clock())


def job_ids(x):
    return [r[0] for r in x.conn.execute("SELECT job_id FROM extraction_jobs ORDER BY job_id")]


class ByTeam:
    """Answers invalidly for the listed teams and validly for every other listing."""

    def __init__(self, *broken):
        self.broken, self.calls = set(broken), []

    def __call__(self, instructions, text, schema):
        self.calls.append(text)
        team = int(re.search(r"team (\d+)", text).group(1))
        return reply(text=INVALID) if team in self.broken else reply()


def in_flight(conn, job_id, started_at, outcome=None):
    """An attempt as another process records it: started, maybe not finished."""
    run_id = conn.execute(
        "INSERT INTO extraction_runs (started_at, config_key) VALUES (%s, %s) RETURNING run_id",
        (started_at, CONFIG.key),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO extraction_attempts (job_id, run_id, attempt_no, model, started_at, "
        "finished_at, outcome, estimated_tokens) VALUES (%s, %s, 1, %s, %s, %s, %s, 4000)",
        (job_id, run_id, CONFIG.model, started_at, started_at if outcome else None, outcome),
    )


# --- lanes ----------------------------------------------------------------------------------


def test_the_new_lane_goes_first_and_kept_listings_lead_each_lane(x):
    with_lanes(x, backlog=[job(1), accountant(2)], new=[job(3), accountant(4)])
    model = ScriptedModel(reply())
    x.run(model)
    assert teams(model) == [3, 4, 1, 2]  # new kept, new other, kept backlog, other backlog


def test_the_backlog_lane_stops_at_its_share_and_new_listings_keep_the_reserve(x):
    config = ExtractionConfig(rpm=60, daily_limit=10, new_reserve=7)  # safety 9, backlog 2
    with_lanes(x, backlog=[job(n) for n in range(1, 5)], new=[job(5)])
    model = ScriptedModel(reply())
    report = x.run(model, config=config)
    assert teams(model) == [5, 1, 2]
    assert report.stop_reason == (
        "backlog share used: 2 requests today; the remaining 7 are kept for new listings"
    )
    x.collect(T0 + 2 * HOUR, [job(n) for n in range(1, 9)])  # three new listings arrive
    model = ScriptedModel(reply())
    x.run(model, config=config)
    assert teams(model) == [6, 7, 8]  # new listings still go through; the backlog waits
    lanes = dict(x.conn.execute("SELECT lane, count(*) FROM extraction_attempts GROUP BY 1"))
    assert lanes == {"new": 4, "backlog": 2}
    info = extraction.status(x.conn, config, x.clock())
    assert (info["backlog_today"], info["backlog_allowance"]) == (2, 2)


def test_the_reserve_is_read_from_the_environment(monkeypatch):
    assert ExtractionConfig.from_env({}).new_reserve == 200
    assert ExtractionConfig.from_env({extraction.NEW_RESERVE_ENV: "0"}).new_reserve == 0
    with pytest.raises(extraction.ExtractionNotConfigured, match="must not be negative"):
        ExtractionConfig.from_env({extraction.NEW_RESERVE_ENV: "-1"})


# --- several processes ----------------------------------------------------------------------


def test_a_job_another_process_holds_is_left_alone(x, other):
    x.collect(T0, [job(1), job(2)])
    enqueue_new(x.conn, CONFIG, x.clock())
    first, second = job_ids(x)
    other.execute("SELECT pg_advisory_lock(%s, %s)", extraction._lock_args(first))
    model = ScriptedModel(reply())
    assert x.run(model).succeeded == 1 and teams(model) == [2]
    assert x.scalar("SELECT status FROM extraction_jobs WHERE job_id = %s", (first,)) == "pending"
    other.execute("SELECT pg_advisory_unlock(%s, %s)", extraction._lock_args(first))
    assert x.run(model).succeeded == 1 and teams(model) == [2, 1]


def test_a_job_left_running_by_a_process_that_ended_is_recovered(x, other):
    x.collect(T0, [job(1)])
    enqueue_new(x.conn, CONFIG, x.clock())
    (job_id,) = job_ids(x)
    x.conn.execute("UPDATE extraction_jobs SET status = 'running'")
    in_flight(x.conn, job_id, x.clock())  # its request never got an answer
    x.clock.advance(timedelta(minutes=5))
    report = x.run(ScriptedModel(reply()))
    assert (report.recovered, report.succeeded) == (1, 1)
    outcomes = [r[0] for r in x.conn.execute(
        "SELECT outcome FROM extraction_attempts ORDER BY attempt_id")]  # fmt: skip
    assert outcomes == ["interrupted", "success"]  # the lost request still counts


def test_a_job_still_running_in_a_live_process_is_not_taken(x, other):
    x.collect(T0, [job(1)])
    enqueue_new(x.conn, CONFIG, x.clock())
    (job_id,) = job_ids(x)
    other.execute("SELECT pg_advisory_lock(%s, %s)", extraction._lock_args(job_id))
    x.conn.execute("UPDATE extraction_jobs SET status = 'running'")
    model = ScriptedModel(reply())
    assert x.run(model).requests == 0 and model.calls == []
    assert x.scalar("SELECT status FROM extraction_jobs") == "running"


def test_pacing_counts_requests_another_process_has_in_flight(x):
    x.collect(T0, [job(1), job(2)])
    enqueue_new(x.conn, CONFIG, x.clock())
    first, _ = job_ids(x)
    in_flight(x.conn, first, x.clock())  # another process just sent a request
    x.run(ScriptedModel(reply()), config=ExtractionConfig(rpm=4, daily_limit=100, new_reserve=0))
    assert 14.0 < x.slept[0] <= 15.0  # 60 s / 4 requests per minute, from that request


def test_the_daily_count_includes_every_process(x):
    x.collect(T0, [job(1)])
    enqueue_new(x.conn, CONFIG, x.clock())
    (job_id,) = job_ids(x)
    for outcome in ("success", "server_error", None):  # finished, failed, still in flight
        in_flight(x.conn, job_id, x.clock(), outcome)
    report = x.run(ScriptedModel(reply()), config=ExtractionConfig(rpm=60, daily_limit=4,
                                                                   new_reserve=0))  # fmt: skip
    assert report.requests == 0 and report.stop_reason.startswith("safety stop: 3 requests")


# --- retries ----------------------------------------------------------------------------------


def test_one_listing_with_bad_answers_does_not_hold_up_the_others(x):
    x.collect(T0, [job(1), job(2), job(3)])
    model = ByTeam(1)
    report = x.run(model)
    assert teams(model)[:3] == [1, 2, 3]  # 2 and 3 went ahead while 1 waited for its retry
    assert teams(model) == [1, 2, 3, 1, 1, 1]
    assert (report.succeeded, report.failed, report.retries_scheduled) == (2, 1, 3)
    status, runs_failed, due = x.conn.execute(
        "SELECT status, runs_failed, next_attempt_at FROM extraction_jobs ORDER BY job_id"
    ).fetchone()
    assert (status, runs_failed) == ("failed", 1)
    assert timedelta(minutes=59) < due - x.clock() <= extraction.FAILED_ROUND_GAP  # waits an hour


def test_a_listing_edited_while_waiting_for_its_retry_is_superseded(x):
    x.collect(T0, [job(1)])
    x.run(ScriptedModel(reply(text=INVALID)), limit=1)  # stops after the first bad answer
    assert x.scalar("SELECT status FROM extraction_jobs") == "pending"
    x.collect(T0 + HOUR, [job(1, content="<p>Rewritten for team 1.</p>")])
    x.clock.advance(timedelta(minutes=1))
    model = ScriptedModel(reply())
    report = x.run(model)
    assert (report.superseded, report.succeeded) == (1, 1)
    assert "Rewritten for team 1." in model.calls[0]
    statuses = [r[0] for r in x.conn.execute("SELECT status FROM extraction_jobs ORDER BY job_id")]
    assert statuses == ["superseded", "succeeded"]


# --- collection -------------------------------------------------------------------------------


def test_collection_queues_after_every_board_while_extraction_is_busy(env, monkeypatch, db_settings,
                                                                      isolated_schema):  # fmt: skip
    worker = other_connection(db_settings, isolated_schema)
    try:
        # Another process is in the middle of a request: it holds the pacing lock.
        worker.execute("SELECT pg_advisory_lock(%s)", (extraction.PACING_LOCK_KEY,))
        calls = []
        real = extraction.enqueue_new
        monkeypatch.setattr(extraction, "enqueue_new",
                            lambda *a: calls.append(1) or real(*a))  # fmt: skip
        env.transport.queue("acme", ok([job(1)]))
        env.transport.queue("beta", ok([job(2)]))
        report = env.collector().collect()
    finally:
        worker.close()
    assert report.status == "complete" and report.extraction_queued == 2
    assert len(calls) == 3  # after each of the two boards, and once at the end
    assert env.scalar("SELECT count(*) FROM extraction_attempts") == 0


def test_a_new_boards_initial_import_keeps_only_kept_listings_in_the_new_lane(x):
    x.track_from(T0 - HOUR)  # tracking started before the board was ever collected
    x.collect(T0, [job(1), accountant(2)])  # the board's first complete scan
    x.collect(T0 + HOUR, [job(1), accountant(2), job(3), accountant(4)])  # new postings
    enqueue_new(x.conn, CONFIG, x.clock())
    assert sorted(x.jobs(), key=lambda r: int(r[0])) == [
        ("1", 1, "new_content", "pending"),  # kept import: read within the day
        ("2", 4, "backlog", "pending"),  # other import: waits with the backlog
        ("3", 1, "new_content", "pending"),
        ("4", 3, "new_content", "pending"),  # posted after the import: a new posting
    ]
    model = ScriptedModel(reply())
    x.run(model)
    assert teams(model) == [1, 3, 4, 2]
