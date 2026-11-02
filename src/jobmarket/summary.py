"""The daily summary email and the heartbeat ping.

The evening pipeline run calls `jobmarket summary send` after its first processing steps
(collection, backup, raw upload and cleanup, report publication, one AI batch). It does not
wait for company retries or the AI backlog: those are reported as pending.

One summary per America/Chicago day: the notification's dedup_key is "summary:<date>", so a
re-run or restart on the same day sends nothing new. The summary covers the time since the
previous summary (or the last 24 hours for the first one). Gemini quota pauses appear here
as information, never as separate alerts.

If JOBMARKET_HEARTBEAT_URL is set, the command requests it after the summary is queued, so
an external monitor notices when the daily run stops happening.
"""

import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import psycopg

from jobmarket import extraction, operations
from jobmarket.search import STALE_AFTER  # the website's "may be out of date" threshold

CHICAGO = ZoneInfo("America/Chicago")
HEARTBEAT_ENV = "JOBMARKET_HEARTBEAT_URL"
FIRST_WINDOW = timedelta(hours=24)


@dataclass
class Summary:
    day: str  # America/Chicago date, YYYY-MM-DD
    subject: str
    body: str
    attention: list[str]  # short reasons the summary needs a look, if any


def _when(value: datetime | None) -> str:
    if value is None:
        return "never"
    t = value.astimezone(CHICAGO)
    return f"{t:%a %b} {t.day}, {t.hour % 12 or 12}:{t:%M} {t:%p} {t:%Z}"


def _since(conn: psycopg.Connection, now: datetime) -> datetime:
    previous = conn.execute(
        "SELECT max(created_at) FROM notifications WHERE kind = 'summary' AND created_at < %s",
        (now,),
    ).fetchone()[0]
    return previous or now - FIRST_WINDOW


def compose(conn: psycopg.Connection, now: datetime) -> Summary:
    since = _since(conn, now)
    day = now.astimezone(CHICAGO).date().isoformat()
    attention: list[str] = []
    lines = [f"Period: {_when(since)} to {_when(now)} (America/Chicago).", ""]

    # --- Collection ------------------------------------------------------------------
    runs = conn.execute(
        "SELECT trigger, status, count(*) FROM collection_runs "
        "WHERE started_at >= %s AND started_at < %s GROUP BY 1, 2 ORDER BY 1, 2",
        (since, now),
    ).fetchall()
    last_run = conn.execute(
        "SELECT finished_at, status FROM collection_runs WHERE finished_at IS NOT NULL "
        "ORDER BY finished_at DESC LIMIT 1"
    ).fetchone()
    lines.append("COLLECTION")
    if runs:
        lines.append("- Runs: " + ", ".join(f"{n} {trig} {st}" for trig, st, n in runs))
    else:
        lines.append("- No collection ran in this period.")
        attention.append("no collection ran")
    lines.append(
        f"- Last run finished: {_when(last_run[0]) if last_run else 'never'}"
        + (f" ({last_run[1]})" if last_run else "")
    )

    new_jobs = conn.execute(
        """
        SELECT e.name, coalesce(ps.title, ''), coalesce(ps.location_text, ''),
               coalesce(ps.retained, false),
               p.first_discovered_at <= (SELECT min(s.finished_at) FROM board_scans s
                                          WHERE s.board_id = p.board_id
                                            AND s.status = 'complete') AS initial_import
        FROM source_postings p
        JOIN source_boards b USING (board_id)
        JOIN employers e USING (employer_id)
        LEFT JOIN posting_screenings ps USING (posting_id)
        WHERE p.first_discovered_at >= %s AND p.first_discovered_at < %s
        ORDER BY e.name, ps.title
        """,
        (since, now),
    ).fetchall()
    discovered = [r for r in new_jobs if not r[4]]
    kept = [r for r in discovered if r[3]]
    imports = len(new_jobs) - len(discovered)
    lines.append(
        f"- New jobs: {len(kept)} kept, {len(discovered)} in all"
        + (f"; {imports} more from newly added boards (initial import)" if imports else "")
    )
    for name, title, location, *_ in kept:
        lines.append(f"    {name}: {title}" + (f" ({location})" if location else ""))

    updates = conn.execute(
        "SELECT count(DISTINCT posting_id) FROM posting_versions "
        "WHERE version_no > 1 AND first_seen_at >= %s AND first_seen_at < %s",
        (since, now),
    ).fetchone()[0]
    moves = Counter(
        dict(
            conn.execute(
                "SELECT to_state, count(*) FROM posting_availability_events "
                "WHERE occurred_at >= %s AND occurred_at < %s GROUP BY 1",
                (since, now),
            ).fetchall()
        )
    )
    lines.append(f"- Listings whose content changed: {updates}")
    if moves:
        lines.append(
            "- Availability changes: "
            + ", ".join(f"{n} to {state.replace('_', ' ')}" for state, n in sorted(moves.items()))
        )

    # --- Source health ----------------------------------------------------------------
    lines += ["", "SOURCE HEALTH"]
    boards, stale, held = conn.execute(
        """
        SELECT count(*),
               count(*) FILTER (WHERE last_complete IS NULL OR last_complete < %s),
               count(*) FILTER (WHERE held)
        FROM (
            SELECT b.board_id,
                   (SELECT max(s.finished_at) FROM board_scans s
                     WHERE s.board_id = b.board_id AND s.status = 'complete') AS last_complete,
                   EXISTS (SELECT 1 FROM board_holds h
                            WHERE h.board_id = b.board_id AND h.status = 'active') AS held
            FROM source_boards b WHERE b.is_active
        ) boards
        """,
        (now - STALE_AFTER,),
    ).fetchone()
    hours = int(STALE_AFTER.total_seconds() // 3600)
    lines.append(f"- Enabled boards: {boards}; no complete collection in {hours} hours: "
                 f"{stale}; on hold: {held}")  # fmt: skip
    if stale:
        attention.append(f"{stale} board(s) out of date")
    incidents = conn.execute(
        """
        SELECT e.name, i.kind, i.status, i.retries_used, i.next_retry_at, i.last_error
        FROM collection_incidents i
        JOIN source_boards b USING (board_id)
        JOIN employers e USING (employer_id)
        WHERE i.status <> 'resolved'
        ORDER BY i.status, i.next_retry_at NULLS LAST, e.name
        """
    ).fetchall()
    if not incidents:
        lines.append("- Open collection problems: none")
    for name, kind, status, used, next_at, error in incidents:
        when = f"next retry {_when(next_at)}" if status == "retrying" else "retries used up"
        lines.append(f"- {name}: {kind} scan, {used} of 3 retries used, {when}. "
                     f"Last error: {(error or 'none recorded')[:160]}")  # fmt: skip
    retrying = [r for r in incidents if r[2] == "retrying"]
    if retrying:
        lines.append(f"- Pending retries: {len(retrying)}")
    if any(r[2] == "exhausted" for r in incidents):
        attention.append("collection retries exhausted")

    # --- AI extraction ----------------------------------------------------------------
    lines += ["", "AI EXTRACTION (requirements)"]
    try:
        config = extraction.ExtractionConfig.from_env()
        info = extraction.status(conn, config, now)
        waiting = Counter()
        for status, priority, n in info["queue"]:
            if status in ("pending", "failed", "running"):
                waiting["kept" if priority in (1, 2) else "other"] += n
        done = conn.execute(
            "SELECT count(*) FROM extraction_results WHERE created_at >= %s AND created_at < %s",
            (since, now),
        ).fetchone()[0]
        lines.append(f"- Extracted in this period: {done}")
        lines.append(
            f"- Waiting: {waiting['kept']} kept and {waiting['other']} other listings"
            f" (+{info['not_yet_queued']} not yet queued)"
        )
        used = info["usage_today"][0]
        lines.append(
            f"- Requests today (Pacific day {info['pacific_day']}): {used}"
            + (f" of the {info['safety_stop_at']} this pipeline allows"
               if info["safety_stop_at"] else "")
        )  # fmt: skip
        if info["paused_until"]:
            lines.append(f"- Paused by the daily free quota until {_when(info['paused_until'])}; "
                         "this is expected and resumes on its own.")  # fmt: skip
    except (extraction.ExtractionNotConfigured, ValueError, psycopg.Error) as exc:
        lines.append(f"- Status unavailable: {exc}")

    # --- Reports ----------------------------------------------------------------------
    lines += ["", "REPORTS"]
    good = operations.last_step_run(conn, "report_publish", "succeeded")
    latest = operations.last_step_run(conn, "report_publish")
    if good:
        lines.append(f"- Published reports built: {good[2].get('built_at', '?')[:16]} UTC "
                     f"(published {_when(good[0])})")  # fmt: skip
    else:
        lines.append("- No reports published yet (the Reports page says 'not ready yet').")
    if latest and latest[1] == "failed":
        lines.append(f"- Latest rebuild FAILED at {_when(latest[0])}: {latest[3][:300]}")
        lines.append("  The website keeps the last good reports.")
        attention.append("report rebuild failed")

    # --- Backups and raw files --------------------------------------------------------
    lines += ["", "BACKUPS"]
    good = operations.last_step_run(conn, "backup", "succeeded")
    latest = operations.last_step_run(conn, "backup")
    if good:
        d = good[2]
        lines.append(f"- Last good backup: {d.get('stamp')} ({d.get('bytes', 0):,} bytes), "
                     f"{'confirmed in S3' if d.get('uploaded') else 'local only'}, "
                     f"finished {_when(good[0])}")  # fmt: skip
        if good[0] < now - FIRST_WINDOW - timedelta(hours=6):
            attention.append("no backup in over a day")
    else:
        lines.append("- No successful backup yet.")
        attention.append("no backup yet")
    if latest and latest[1] == "failed":
        lines.append(f"- Latest backup FAILED at {_when(latest[0])}: {latest[3][:300]}")
        attention.append("backup failed")
    prune = operations.last_step_run(conn, "backup_prune", "succeeded")
    if prune:
        d = prune[2]
        text = f"- Local copies kept: {len(d.get('kept', []))}"
        if d.get("kept_not_uploaded"):
            text += f"; not yet confirmed in S3: {len(d['kept_not_uploaded'])}"
        if d.get("damaged"):
            text += f"; damaged: {', '.join(d['damaged'])}"
        lines.append(text)
    sync = operations.last_step_run(conn, "raw_sync")
    cleanup = operations.last_step_run(conn, "raw_cleanup")
    if sync:
        lines.append(f"- Raw files to S3: {sync[1]} at {_when(sync[0])}"
                     + (f" ({sync[2].get('uploaded', 0)} uploaded)" if sync[1] == "succeeded"
                        else f": {(sync[3] or '')[:200]}"))  # fmt: skip
    if cleanup and cleanup[1] == "succeeded" and cleanup[2].get("kept_not_uploaded"):
        lines.append(f"- Old raw files kept until S3 confirms them: "
                     f"{cleanup[2]['kept_not_uploaded']}")  # fmt: skip

    # --- Outbox -----------------------------------------------------------------------
    stuck = conn.execute(
        "SELECT count(*) FROM notifications WHERE status = 'pending' AND attempts > 0"
    ).fetchone()[0]
    if stuck:
        lines += ["", f"EMAIL: {stuck} earlier message(s) could not be delivered yet."]

    weekday = now.astimezone(CHICAGO)
    subject = (f"[Job Pipeline] Daily summary {weekday:%a %b} {weekday.day}: "
               f"{len(kept)} new kept job{'s' * (len(kept) != 1)}")  # fmt: skip
    if attention:
        subject += " (needs a look)"
        lines = ["Needs a look: " + "; ".join(attention) + ".", ""] + lines
    return Summary(day, subject, "\n".join(lines) + "\n", attention)


def queue_daily(conn: psycopg.Connection, now: datetime) -> tuple[Summary, int | None]:
    """Compose and queue today's summary; the id is None when today's was already queued."""
    summary = compose(conn, now)
    notification_id = operations.queue_notification(
        conn, "summary", summary.subject, summary.body, now, dedup_key=f"summary:{summary.day}"
    )
    return summary, notification_id


def ping_heartbeat(environ=None, client_factory=httpx.Client) -> str | None:
    """Request the heartbeat URL, if configured. Returns an error message, or None."""
    env = os.environ if environ is None else environ
    url = env.get(HEARTBEAT_ENV, "").strip()
    if not url:
        return None
    try:
        with client_factory(timeout=10.0) as client:
            client.get(url).raise_for_status()
    except httpx.HTTPError as exc:
        return f"heartbeat failed: {type(exc).__name__}"
    return None
