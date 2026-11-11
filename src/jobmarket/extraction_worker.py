"""The extraction worker: a long-running process that extracts new listings within minutes
of their collection, instead of waiting for the next hourly batch.

It works on the same PostgreSQL queue as `jobmarket extract run` (jobmarket.extraction),
with the same lanes, limits, retries, and result checks; only the loop around them is new.

- One job at a time. Collection queues new content after every board; the worker claims
  the next ready job as soon as it is free. When nothing is ready it queues any content that
  is still missing a job and checks again after IDLE_SECONDS (15 s, decision 2026-11-05).
  Waiting is a sleep on an event, never a busy loop.
- Pauses. After a daily-quota error or at the safety stop it sleeps until the provider's
  reset (midnight Pacific), and through an outage pause (15 minutes). After a rejected
  request (such as a bad key) it waits REJECTED_PAUSE and tries again.
- One worker at a time: it holds the advisory lock WORKER_LOCK_KEY for its whole life. The
  hourly batch (`extract run --skip-if-worker`) skips while that lock is held, and runs
  again by itself as soon as the worker stops, which is the rollback path.
- Shutdown. SIGTERM or SIGINT sets a flag. A request already sent is allowed to finish and
  is recorded; waits end at once and the job goes back to the queue. If the process is
  killed anyway, the next claim marks the unanswered request interrupted (it still counts
  toward the day's requests) and picks the job up again.
- Heartbeat. Each worker process keeps a row in extraction_workers with its state and the
  reason for a pause, updated at every change and at least every idle check.
"""

import os
import signal
import socket
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

import psycopg

from jobmarket.clock import Clock
from jobmarket.extraction import (
    ExtractionBusy,
    ExtractionConfig,
    Generator,
    RunReport,
    Session,
    StopRun,
    enqueue_new,
)

WORKER_LOCK_KEY = 4_815_162_346
IDLE_SECONDS = 15.0  # how often an idle worker checks for new work (decision 2026-11-05)
REJECTED_PAUSE = timedelta(hours=1)  # wait after a rejected request before trying again


class Stopper:
    """A stop flag that interrupts waits, set by SIGTERM or SIGINT."""

    def __init__(self):
        self._event = threading.Event()

    def install(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self._event.set())

    def stop(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, seconds: float) -> None:
        self._event.wait(max(0.0, seconds))


def worker_id() -> str:
    return f"worker@{socket.gethostname()}:{os.getpid()}"


def worker_running(conn: psycopg.Connection) -> bool:
    """Whether an extraction worker holds its lock (that is, is alive) right now."""
    if conn.execute("SELECT pg_try_advisory_lock(%s)", (WORKER_LOCK_KEY,)).fetchone()[0]:
        conn.execute("SELECT pg_advisory_unlock(%s)", (WORKER_LOCK_KEY,))
        return False
    return True


def beat(conn, worker: str, now: datetime, state: str, detail: str | None = None,
         run_id: int | None = None) -> None:  # fmt: skip
    conn.execute(
        """
        INSERT INTO extraction_workers (worker_id, started_at, heartbeat_at, state, detail,
                                        run_id, stopped_at)
        VALUES (%(w)s, %(now)s, %(now)s, %(state)s, %(detail)s, %(run)s,
                CASE WHEN %(state)s = 'stopped' THEN %(now)s END)
        ON CONFLICT (worker_id) DO UPDATE SET heartbeat_at = EXCLUDED.heartbeat_at,
            state = EXCLUDED.state, detail = EXCLUDED.detail,
            run_id = coalesce(EXCLUDED.run_id, extraction_workers.run_id),
            stopped_at = EXCLUDED.stopped_at
        """,
        {"w": worker, "now": now, "state": state, "detail": detail, "run": run_id},
    )


def latest_worker(conn: psycopg.Connection) -> tuple | None:
    """(worker_id, state, detail, heartbeat_at, started_at) of the most recent worker."""
    return conn.execute(
        "SELECT worker_id, state, detail, heartbeat_at, started_at FROM extraction_workers "
        "ORDER BY heartbeat_at DESC LIMIT 1"
    ).fetchone()


def _resume_at(stop: StopRun, now: datetime) -> datetime:
    return stop.until or now + REJECTED_PAUSE


def run_worker(
    conn: psycopg.Connection,
    generate: Generator,
    config: ExtractionConfig,
    clock: Clock,
    stopper: Stopper,
    worker: str,
    log: Callable[[str], None] = print,
    max_loops: int | None = None,
) -> RunReport:
    """Extract until stopped (see the module docstring). conn must be in autocommit mode.
    max_loops bounds the loop for tests."""
    config.require_limits()
    if not conn.execute("SELECT pg_try_advisory_lock(%s)", (WORKER_LOCK_KEY,)).fetchone()[0]:
        raise ExtractionBusy("another extraction worker is running")
    session = Session(conn, generate, config, clock, stopper.wait, worker,
                      should_stop=stopper.is_set)  # fmt: skip
    report = session.report
    try:
        beat(conn, worker, clock(), "starting")
        session.start()
        beat(conn, worker, clock(), "idle", "started", report.run_id)
        log(f"Extraction worker {worker} started ({config.key}); run {report.run_id}.")
        state = None
        loops = 0
        while not stopper.is_set() and (max_loops is None or loops < max_loops):
            loops += 1
            before = (report.succeeded, report.reused, report.superseded, report.failed,
                      report.retries_scheduled)  # fmt: skip
            try:
                job_id = session.step()
            except StopRun as stop:
                if stopper.is_set():
                    break
                until = _resume_at(stop, clock())
                beat(conn, worker, clock(), "paused", stop.reason)
                session.save(finished=False)
                if state != ("paused", stop.reason):
                    log(
                        f"Paused: {stop.reason}. Resuming at {until.isoformat(timespec='minutes')}."
                    )
                    state = ("paused", stop.reason)
                _sleep_until(conn, worker, clock, stopper, until, stop.reason)
                continue
            if job_id is not None:
                after = (report.succeeded, report.reused, report.superseded, report.failed,
                         report.retries_scheduled)  # fmt: skip
                what = _what_happened(before, after)
                beat(conn, worker, clock(), "working", f"job {job_id}: {what}")
                session.save(finished=False)
                log(f"Job {job_id}: {what}.")
                state = ("working", None)
                continue
            with conn.transaction():
                queued = enqueue_new(conn, config, clock())
            if queued:
                log(f"Queued {queued} listing(s) that had no job yet.")
                continue
            detail = (
                session.backlog_message()
                if not session.backlog_open and session.backlog_waiting()
                else "queue empty"
            )
            beat(conn, worker, clock(), "idle", detail)
            if state != ("idle", detail):
                log(f"Idle: {detail}.")
                state = ("idle", detail)
            wait = session.next_retry_in()
            stopper.wait(min(IDLE_SECONDS, wait) if wait is not None else IDLE_SECONDS)
        report.stop_reason = "stopped" if stopper.is_set() else "loop limit reached"
    finally:
        session.save()
        beat(conn, worker, clock(), "stopped", report.stop_reason)
        conn.execute("SELECT pg_advisory_unlock(%s)", (WORKER_LOCK_KEY,))
    log(
        f"Extraction worker stopped: {report.requests} request(s), {report.succeeded} "
        f"extracted, {report.reused} reused, {report.failed} failed."
    )
    return report


def _sleep_until(conn, worker, clock, stopper, until: datetime, reason: str) -> None:
    """Sleep until `until` in idle-check steps, keeping the heartbeat fresh."""
    while not stopper.is_set():
        left = (until - clock()).total_seconds()
        if left <= 0:
            return
        stopper.wait(min(IDLE_SECONDS, left))
        beat(conn, worker, clock(), "paused", reason)


def _what_happened(before: tuple, after: tuple) -> str:
    names = ("extracted", "reused an identical earlier answer", "superseded by newer content",
             "gave up", "failed a try, retry scheduled")  # fmt: skip
    changed = [name for name, a, b in zip(names, before, after, strict=True) if b > a]
    return ", ".join(changed) or "released"


def run_unconfigured(conn, worker: str, clock: Clock, stopper: Stopper, problem: str) -> None:
    """Without a key or limits the worker records why and waits to be stopped, rather than
    exiting and being restarted in a loop. It holds the worker lock meanwhile so it shows as
    alive; the hourly batch reads the same settings, so it could not extract either."""
    locked = conn.execute("SELECT pg_try_advisory_lock(%s)", (WORKER_LOCK_KEY,)).fetchone()[0]
    try:
        beat(conn, worker, clock(), "paused", f"not configured: {problem}")
        while not stopper.is_set():
            stopper.wait(IDLE_SECONDS)
            beat(conn, worker, clock(), "paused", f"not configured: {problem}")
        beat(conn, worker, clock(), "stopped", "stopped while not configured")
    finally:
        if locked:
            conn.execute("SELECT pg_advisory_unlock(%s)", (WORKER_LOCK_KEY,))
