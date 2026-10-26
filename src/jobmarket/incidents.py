"""Retry budget and incidents for unsuccessful board scans.

A failed or partial scan opens an incident with three retries due about one, two, and
three hours after the first failure. Retry state lives in the database, so restarts do
not lose or reset it, and nothing waits in-process for hours: `jobmarket retry-due` (run
manually now, by a scheduler later) performs whatever retries are due.

Attempt kinds:
  initial  no open incident when the attempt started
  retry    the incident's next retry was due; this attempt uses one retry
  extra    an incident is open but no retry is due yet (for example a scheduled run in
           the middle of the retry window); success still resolves the incident, but a
           failure neither uses nor resets the retry budget

An alert is queued only when the retries are exhausted, once per incident. A recovery
notice is queued when an incident that was alerted is resolved.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg

MAX_RETRIES = 3
RETRY_SPACING = timedelta(hours=1)
# If retries run late (no scheduler yet), keep at least this gap between attempts.
MIN_RETRY_GAP = timedelta(minutes=15)


@dataclass(frozen=True)
class OpenIncident:
    incident_id: int
    status: str
    retries_used: int
    next_retry_at: datetime | None


def open_incident(conn: psycopg.Connection, board_id: int) -> OpenIncident | None:
    row = conn.execute(
        "SELECT incident_id, status, retries_used, next_retry_at FROM collection_incidents "
        "WHERE board_id = %s AND status <> 'resolved'",
        (board_id,),
    ).fetchone()
    return OpenIncident(*row) if row else None


def attempt_kind(incident: OpenIncident | None, now: datetime) -> str:
    if incident is None:
        return "initial"
    if incident.status == "retrying" and incident.next_retry_at <= now:
        return "retry"
    return "extra"


@dataclass(frozen=True)
class IncidentEvent:
    """What changed for a board after a scan. kind is one of: opened, retry_failed,
    exhausted, still_failing, resolved, resolved_after_alert, none."""

    kind: str
    incident_id: int | None


def record_outcome(
    conn: psycopg.Connection,
    board_id: int,
    scan_id: int,
    status: str,
    error: str | None,
    kind_of_attempt: str,
    now: datetime,
    retry_after_seconds: float | None = None,
) -> IncidentEvent:
    """Update the board's incident after a scan. Runs inside the caller's transaction."""
    row = conn.execute(
        "SELECT incident_id, status, retries_used, opened_at, alert_notification_id "
        "FROM collection_incidents WHERE board_id = %s AND status <> 'resolved' FOR UPDATE",
        (board_id,),
    ).fetchone()

    if status == "complete":
        if row is None:
            return IncidentEvent("none", None)
        incident_id, _, _, _, alert_id = row
        conn.execute(
            "UPDATE collection_incidents SET status = 'resolved', next_retry_at = NULL, "
            "resolved_at = %s, resolved_by_scan_id = %s, last_scan_id = %s "
            "WHERE incident_id = %s",
            (now, scan_id, scan_id, incident_id),
        )
        return IncidentEvent("resolved_after_alert" if alert_id else "resolved", incident_id)

    if row is None:
        next_retry = _respect_retry_after(now + RETRY_SPACING, now, retry_after_seconds)
        incident_id = conn.execute(
            "INSERT INTO collection_incidents (board_id, opened_at, opened_by_scan_id, kind, "
            "status, retries_used, next_retry_at, last_scan_id, last_error) "
            "VALUES (%s, %s, %s, %s, 'retrying', 0, %s, %s, %s) RETURNING incident_id",
            (board_id, now, scan_id, status, next_retry, scan_id, error),
        ).fetchone()[0]
        return IncidentEvent("opened", incident_id)

    incident_id, inc_status, retries_used, opened_at, _ = row
    conn.execute(
        "UPDATE collection_incidents SET kind = %s, last_scan_id = %s, last_error = %s "
        "WHERE incident_id = %s",
        (status, scan_id, error, incident_id),
    )
    if inc_status == "retrying" and kind_of_attempt == "retry":
        retries_used += 1
        if retries_used >= MAX_RETRIES:
            conn.execute(
                "UPDATE collection_incidents SET retries_used = %s, status = 'exhausted', "
                "next_retry_at = NULL WHERE incident_id = %s",
                (retries_used, incident_id),
            )
            return IncidentEvent("exhausted", incident_id)
        planned = opened_at + RETRY_SPACING * (retries_used + 1)
        next_retry = _respect_retry_after(
            max(planned, now + MIN_RETRY_GAP), now, retry_after_seconds
        )
        conn.execute(
            "UPDATE collection_incidents SET retries_used = %s, next_retry_at = %s "
            "WHERE incident_id = %s",
            (retries_used, next_retry, incident_id),
        )
        return IncidentEvent("retry_failed", incident_id)
    return IncidentEvent("still_failing", incident_id)


def _respect_retry_after(
    planned: datetime, now: datetime, retry_after_seconds: float | None
) -> datetime:
    if retry_after_seconds is None:
        return planned
    return max(planned, now + timedelta(seconds=retry_after_seconds))


def due_retry_board_ids(conn: psycopg.Connection, now: datetime) -> list[int]:
    rows = conn.execute(
        "SELECT i.board_id FROM collection_incidents i JOIN source_boards b USING (board_id) "
        "WHERE i.status = 'retrying' AND i.next_retry_at <= %s AND b.is_active "
        "ORDER BY i.next_retry_at, i.board_id",
        (now,),
    ).fetchall()
    return [board_id for (board_id,) in rows]


def next_retry_time(conn: psycopg.Connection) -> datetime | None:
    return conn.execute(
        "SELECT min(next_retry_at) FROM collection_incidents WHERE status = 'retrying'"
    ).fetchone()[0]


# --- Notifications -------------------------------------------------------------------


def _incident_lines(conn: psycopg.Connection, incident_ids: list[int]) -> list[str]:
    rows = conn.execute(
        """
        SELECT e.name, b.board_key, i.kind, i.last_error, i.retries_used, i.opened_at,
               i.resolved_at,
               (SELECT max(s.finished_at) FROM board_scans s
                 WHERE s.board_id = b.board_id AND s.status = 'complete') AS last_complete,
               (SELECT max(s.finished_at) FROM board_scans s
                 WHERE s.board_id = b.board_id AND s.status IN ('complete', 'partial'))
                   AS last_usable,
               (SELECT count(*) FROM source_postings p JOIN posting_screenings ps
                  USING (posting_id) WHERE p.board_id = b.board_id AND ps.retained)
                   AS retained
        FROM collection_incidents i
        JOIN source_boards b USING (board_id)
        JOIN employers e USING (employer_id)
        WHERE i.incident_id = ANY(%s)
        ORDER BY e.name
        """,
        (incident_ids,),
    ).fetchall()
    lines = []
    for (
        name,
        token,
        kind,
        error,
        retries,
        opened,
        resolved,
        last_complete,
        last_usable,
        retained,
    ) in rows:
        lines.append(f"- {name} (board '{token}')")
        lines.append(f"    problem: {kind} scan; last error: {error or 'none recorded'}")
        lines.append(f"    first failure: {_fmt(opened)}; retries used: {retries} of {MAX_RETRIES}")
        if resolved:
            lines.append(f"    recovered: {_fmt(resolved)}")
        lines.append(
            f"    last complete collection: {_fmt(last_complete)}; "
            f"last usable collection: {_fmt(last_usable)}"
        )
        lines.append(
            f"    still usable: {retained} retained job(s) from earlier scans; "
            "nothing was closed or deleted"
        )
    return lines


def _fmt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M %Z") if value else "never"


def queue_notifications(
    conn: psycopg.Connection,
    run_id: int | None,
    run_status: str,
    events: list[IncidentEvent],
    now: datetime,
) -> list[int]:
    """Queue at most one alert and one recovery notice for this run. Runs inside the
    caller's transaction. Returns the new notification ids."""
    created = []
    exhausted = [e.incident_id for e in events if e.kind == "exhausted"]
    if exhausted:
        pending = [
            i
            for (i,) in conn.execute(
                "SELECT incident_id FROM collection_incidents WHERE incident_id = ANY(%s) "
                "AND alert_notification_id IS NULL",
                (exhausted,),
            )
        ]
        if pending:
            whole_run = run_status == "failed"
            subject = (
                f"[Job Pipeline] Collection failed for {len(pending)} employer(s) after 3 retries"
            )
            body = "\n".join(
                [
                    "Collection is still unsuccessful after all retries.",
                    (
                        f"Run {run_id} finished with status '{run_status}'"
                        + (" (every board in this run failed)." if whole_run else ".")
                    )
                    if run_id is not None
                    else "Detected while recovering an abandoned run.",
                    "",
                    *_incident_lines(conn, pending),
                    "",
                    "No more automatic retries are scheduled for these incidents. Regular runs "
                    "keep trying; you will get one recovery notice when collection succeeds.",
                ]
            )
            notification_id = _insert_notification(conn, "alert", subject, body, now)
            conn.execute(
                "UPDATE collection_incidents SET alert_notification_id = %s "
                "WHERE incident_id = ANY(%s)",
                (notification_id, pending),
            )
            created.append(notification_id)

    recovered = [e.incident_id for e in events if e.kind == "resolved_after_alert"]
    if recovered:
        subject = f"[Job Pipeline] Collection recovered for {len(recovered)} employer(s)"
        body = "\n".join(
            [
                "Collection succeeded again for boards that were previously alerted.",
                "",
                *_incident_lines(conn, recovered),
            ]
        )
        notification_id = _insert_notification(conn, "recovery", subject, body, now)
        conn.execute(
            "UPDATE collection_incidents SET recovery_notification_id = %s "
            "WHERE incident_id = ANY(%s)",
            (notification_id, recovered),
        )
        created.append(notification_id)
    return created


def _insert_notification(
    conn: psycopg.Connection, kind: str, subject: str, body: str, now: datetime
) -> int:
    return conn.execute(
        "INSERT INTO notifications (kind, subject, body, created_at) VALUES (%s, %s, %s, %s) "
        "RETURNING notification_id",
        (kind, subject, body, now),
    ).fetchone()[0]
