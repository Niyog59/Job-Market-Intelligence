"""Pipeline step records and alerts for steps other than collection.

Every scheduled step (report publication, backups, raw uploads and cleanup, the daily
summary) records one pipeline_step_runs row. For the steps that alert (report publication
and backups) a failure opens an incident in ops_incidents and queues one alert; further
failures of the same step only update that incident; the next success resolves it and queues
one recovery notice. The notification rows carry a unique dedup_key, so a step that is run
twice (a retry, a restart) never queues the same message twice.

Collection failures are not handled here: each board has its own retry incident
(jobmarket.incidents), which alerts only after its three retries are exhausted.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

ALERTING_STEPS = {
    "report_publish": (
        "Report publication failed",
        "The website keeps showing the last good reports and their build time. Job search is "
        "unaffected and newly collected data is kept. The next scheduled run tries again.",
    ),
    "backup": (
        "Backup failed",
        "Earlier backups are untouched. The next evening run tries again; until then the "
        "newest good backup is the one listed in the daily summary.",
    ),
}
STEPS = ("report_publish", "backup", "backup_prune", "raw_sync", "raw_cleanup", "daily_summary")


@dataclass(frozen=True)
class StepOutcome:
    step_run_id: int
    alert_notification_id: int | None = None
    recovery_notification_id: int | None = None


def queue_notification(
    conn: psycopg.Connection,
    kind: str,
    subject: str,
    body: str,
    now: datetime,
    dedup_key: str | None = None,
) -> int | None:
    """Insert into the outbox; None when a notification with this dedup_key already exists."""
    row = conn.execute(
        "INSERT INTO notifications (kind, subject, body, created_at, dedup_key) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (dedup_key) DO NOTHING "
        "RETURNING notification_id",
        (kind, subject, body, now, dedup_key),
    ).fetchone()
    return row[0] if row else None


def record_step(
    conn: psycopg.Connection,
    step: str,
    started_at: datetime,
    finished_at: datetime,
    status: str,
    detail: dict[str, Any] | None = None,
    error: str | None = None,
) -> StepOutcome:
    """Record one run of a step and, for alerting steps, open, update, or resolve its
    incident. Everything happens in one transaction."""
    if step not in STEPS:
        raise ValueError(f"unknown pipeline step {step!r}")
    if (status == "failed") != (error is not None):
        raise ValueError("a failed step needs an error message, and only a failed step")
    with conn.transaction():
        step_run_id = conn.execute(
            "INSERT INTO pipeline_step_runs (step, started_at, finished_at, status, detail, "
            "error) VALUES (%s, %s, %s, %s, %s, %s) RETURNING step_run_id",
            (step, started_at, finished_at, status, Jsonb(detail or {}), error),
        ).fetchone()[0]
        if step not in ALERTING_STEPS or status == "skipped":
            return StepOutcome(step_run_id)
        if status == "failed":
            return StepOutcome(step_run_id, _failed(conn, step, error, finished_at))
        return StepOutcome(step_run_id, None, _succeeded(conn, step, finished_at))


def _failed(conn, step: str, error: str, now: datetime) -> int | None:
    row = conn.execute(
        "SELECT incident_id FROM ops_incidents WHERE step = %s AND resolved_at IS NULL FOR UPDATE",
        (step,),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE ops_incidents SET failures = failures + 1, last_failure_at = %s, "
            "last_error = %s WHERE incident_id = %s",
            (now, error[:2000], row[0]),
        )
        return None
    incident_id = conn.execute(
        "INSERT INTO ops_incidents (step, opened_at, last_failure_at, last_error) "
        "VALUES (%s, %s, %s, %s) RETURNING incident_id",
        (step, now, now, error[:2000]),
    ).fetchone()[0]
    title, consequence = ALERTING_STEPS[step]
    notification_id = queue_notification(
        conn,
        "alert",
        f"[Job Pipeline] {title}",
        f"{title} at {now.isoformat(timespec='minutes')}.\n\nError:\n{error[:2000]}\n\n"
        f"{consequence}\n\nYou will get one recovery notice when it succeeds again.",
        now,
        dedup_key=f"ops-alert:{incident_id}",
    )
    conn.execute(
        "UPDATE ops_incidents SET alert_notification_id = %s WHERE incident_id = %s",
        (notification_id, incident_id),
    )
    return notification_id


def _succeeded(conn, step: str, now: datetime) -> int | None:
    row = conn.execute(
        "SELECT incident_id, opened_at, failures, alert_notification_id FROM ops_incidents "
        "WHERE step = %s AND resolved_at IS NULL FOR UPDATE",
        (step,),
    ).fetchone()
    if not row:
        return None
    incident_id, opened_at, failures, alert_id = row
    notification_id = None
    if alert_id is not None:
        title = ALERTING_STEPS[step][0].replace("failed", "recovered")
        notification_id = queue_notification(
            conn,
            "recovery",
            f"[Job Pipeline] {title}",
            f"{title} at {now.isoformat(timespec='minutes')}, after {failures} failed "
            f"attempt(s) since {opened_at.isoformat(timespec='minutes')}.",
            now,
            dedup_key=f"ops-recovery:{incident_id}",
        )
    conn.execute(
        "UPDATE ops_incidents SET resolved_at = %s, recovery_notification_id = %s "
        "WHERE incident_id = %s",
        (now, notification_id, incident_id),
    )
    return notification_id


def last_step_run(conn: psycopg.Connection, step: str, status: str | None = None):
    """(finished_at, status, detail, error) of the latest run of a step, or None."""
    where, params = "step = %s", [step]
    if status:
        where += " AND status = %s"
        params.append(status)
    return conn.execute(
        f"SELECT finished_at, status, detail, error FROM pipeline_step_runs WHERE {where} "
        "ORDER BY finished_at DESC, step_run_id DESC LIMIT 1",
        params,
    ).fetchone()


def open_incidents(conn: psycopg.Connection) -> list[tuple]:
    """(step, opened_at, failures, last_error) for every unresolved step incident."""
    return conn.execute(
        "SELECT step, opened_at, failures, last_error FROM ops_incidents "
        "WHERE resolved_at IS NULL ORDER BY opened_at"
    ).fetchall()
