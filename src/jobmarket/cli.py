"""Command-line entry point: ``jobmarket <command>``."""

import argparse
import json
import os
import shutil
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg

from jobmarket import db, extraction, extraction_worker
from jobmarket.availability import ACTIVE_STATES, AvailabilityError, close_posting, release_hold
from jobmarket.clock import system_clock
from jobmarket.collector import (
    CollectionBusy,
    Collector,
    concurrency,
    make_client,
    request_interval,
    summarize,
)
from jobmarket.config import ConfigError, Settings, load_settings
from jobmarket.connectors import Pacer
from jobmarket.duplicates import ReviewError, confirm_flag, dismiss_flag, dissolve_group
from jobmarket.evidence import (
    check_mappings,
    check_pages,
    make_page_client,
    mapping_targets,
    page_targets,
)
from jobmarket.incidents import next_retry_time
from jobmarket.listings import listing_detail, plain_text, summary_lines
from jobmarket.migrate import MigrationError, apply_migrations, migration_status
from jobmarket.notify import send_pending, smtp_settings
from jobmarket.raw_store import RAW_RETENTION, cleanup_raw, raw_dir
from jobmarket.replay import UnknownScan, available_scan_ids, replay_scans
from jobmarket.sources import RegistryError, load_registry, registry_paths, sync_registry
from jobmarket.store import refingerprint_versions

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG_ERROR = 2
EXIT_BUSY = 3

DISPLAY_TZ = ZoneInfo("America/Chicago")


def _describe(settings: Settings) -> str:
    return f"{settings.db_user}@{settings.db_host}:{settings.db_port}/{settings.db_name}"


def _when(value: datetime | None) -> str:
    return value.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M %Z") if value else "never"


def _check_db(settings: Settings, args) -> int:
    try:
        info = db.check_connection(settings)
    except psycopg.Error as exc:
        print(f"Database check failed for {_describe(settings)}: {exc}".rstrip(), file=sys.stderr)
        return EXIT_FAILURE
    print(
        f"OK: connected to database {info.database!r} as {info.user!r} "
        f"(PostgreSQL {info.server_version})"
    )
    return EXIT_OK


def _migrate(settings: Settings, args) -> int:
    with db.connect(settings, autocommit=True) as conn:
        if args.status:
            for item in migration_status(conn):
                state = item.applied_at.isoformat() if item.applied_at else "pending"
                print(f"{item.filename}  {state}")
            return EXIT_OK
        applied = apply_migrations(conn)
    if not applied:
        print("Database schema is up to date.")
    for filename in applied:
        print(f"Applied {filename}")
    return EXIT_OK


def _check_mapping(settings: Settings, args) -> int:
    with db.connect(settings, autocommit=True) as conn, make_page_client() as client:
        targets = mapping_targets(conn, args.all, args.source, args.board)
        results = check_mappings(conn, client, targets, system_clock, Pacer(request_interval()))
    for r in results:
        t = r.target
        status = "-" if r.http_status is None else r.http_status
        print(f"{t.source_system:10} {t.token:26} {r.outcome:12} {status!s:>4}  {t.url}")
        if r.matched:
            print(f"    matched: {r.matched}")
        if r.detail:
            print(f"    {r.detail}")
    tally = {}
    for r in results:
        tally[r.outcome] = tally.get(r.outcome, 0) + 1
    print(
        f"Checked {len(results)} official page(s): "
        + (", ".join(f"{n} {o}" for o, n in sorted(tally.items())) or "none")
        + ". Results are stored in board_mapping_checks; verification status is unchanged."
    )
    return EXIT_OK


def _check_pages(settings: Settings, args) -> int:
    with db.connect(settings, autocommit=True) as conn, make_page_client() as client:
        targets = page_targets(conn, args.per_board, args.source, args.board)
        results = check_pages(conn, client, targets, system_clock, Pacer(request_interval()))
    for r in results:
        t = r.target
        status = "-" if r.http_status is None else r.http_status
        print(f"{t.employer[:28]:28} {t.url_kind:11} {r.outcome:12} {status!s:>4}  {t.title[:50]}")
        if r.detail:
            print(f"    {r.detail}")
    tally = {}
    for r in results:
        tally[r.outcome] = tally.get(r.outcome, 0) + 1
    print(
        f"Checked {len(results)} sampled page(s) (GET only; nothing was submitted): "
        + (", ".join(f"{n} {o}" for o, n in sorted(tally.items())) or "none")
        + ". This is a sample, not a check of every posting. Stored in posting_page_checks."
    )
    return EXIT_OK


def _sources(settings: Settings, args) -> int:
    if args.sources_command == "check-mapping":
        return _check_mapping(settings, args)
    registry = load_registry()
    if args.sources_command == "sync":
        with db.connect(settings, autocommit=True) as conn:
            result = sync_registry(conn, registry, system_clock())
        files = ", ".join(str(p) for p in registry_paths())
        print(
            f"Synced {files}: {result.boards_upserted} boards "
            f"({result.boards_enabled} enabled) for {result.employers_upserted} employers."
        )
        if result.boards_not_in_registry:
            print(
                "Disabled boards no longer in the registry: "
                + ", ".join(result.boards_not_in_registry)
            )
        return EXIT_OK

    boards = registry.boards if args.all else registry.enabled
    print(f"{'source':10} {'token':28} {'employer':34} {'region':6} {'status':9} metros")
    for b in boards:
        state = "enabled" if b.enabled else b.verification
        print(
            f"{b.source_system:10} {b.token:28} {b.employer[:34]:34} {b.region:6} {state:9} "
            f"{','.join(b.metros) or '-'}"
        )
    if args.all:
        for source in registry.source_systems:
            mine = [b for b in registry.boards if b.source_system == source]
            enabled = sum(b.enabled for b in mine)
            disabled = len(mine) - enabled
            print(f"\n{source}: {len(mine)} boards, {enabled} enabled, {disabled} disabled.")
        print("\nUnverified leads and holds (not collected):")
        for lead in registry.leads:
            tokens = ", ".join(lead.tokens) or "token not found yet"
            print(
                f"  [{lead.status}] {lead.source_system} {lead.employer}: {tokens}"
                + (f" ({lead.notes})" if lead.notes else "")
            )
    return EXIT_OK


def _collect(settings: Settings, args) -> int:
    with db.connect(settings, autocommit=True) as conn, make_client() as client:
        collector = Collector(
            conn,
            client,
            raw_dir(),
            interval=request_interval(),
            workers_per_source=concurrency(),
        )
        try:
            if args.command == "retry-due":
                report = collector.retry_due()
            else:
                trigger = "scheduled" if args.scheduled else "manual"
                report = collector.collect(trigger, args.board or None)
        except CollectionBusy as exc:
            print(f"Not started: {exc}.", file=sys.stderr)
            return EXIT_BUSY
        except ValueError as exc:
            print(f"Not started: {exc}.", file=sys.stderr)
            return EXIT_FAILURE
        for line in summarize(report):
            print(line)
        if args.command == "retry-due" and report.run_id is None:
            nxt = next_retry_time(conn)
            print(
                f"No retries are due. Next retry: {_when(nxt)}."
                if nxt
                else "No retries are due and none are scheduled."
            )
    if report.status == "failed":
        return EXIT_FAILURE
    return EXIT_OK


def _status(settings: Settings, args) -> int:
    with db.connect(settings) as conn:
        rows = conn.execute(
            "SELECT employer, source_system, token, enabled, last_scan_status, last_attempt_at, "
            "last_attempt_kind, last_scan_pages, last_complete_at, retained_postings, "
            "incident_status, incident_kind, retries_used, next_retry_at, retry_due, "
            "incident_attempts, alerted, last_error, hold_id, hold_missing_count, "
            "hold_reference_count, last_trusted_check_at FROM board_status "
            + ("" if args.all else "WHERE enabled ")
            + "ORDER BY employer, source_system, token"
        ).fetchall()
        pending = conn.execute(
            "SELECT count(*) FROM notifications WHERE status = 'pending'"
        ).fetchone()[0]
        runs = conn.execute(
            "SELECT run_id, trigger, status, started_at FROM collection_runs "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()

    print("Collection status (implemented; not scheduled: runs happen only when a command is run).")
    if runs:
        print(f"Latest run: {runs[0]} ({runs[1]}) {runs[2]}, started {_when(runs[3])}")
    print(
        f"{'employer':30} {'source':10} {'token':24} {'last':8} {'attempt':21} {'pages':>5} "
        f"{'last complete':21} {'kept':>5}  incident"
    )
    for (
        employer,
        source,
        token,
        enabled,
        last,
        attempted,
        kind,
        pages,
        complete,
        retained,
        inc_status,
        inc_kind,
        retries,
        next_retry,
        due,
        attempts,
        alerted,
        error,
        hold_id,
        hold_missing,
        hold_reference,
        trusted_at,
    ) in rows:
        incident = "-"
        if inc_status == "retrying":
            when = "due now" if due else f"next {_when(next_retry)}"
            incident = (
                f"{inc_kind}: retrying ({retries}/3 retries used, {attempts} attempt(s), {when})"
            )
        elif inc_status == "exhausted":
            incident = (
                f"{inc_kind}: retries exhausted after {attempts} attempt(s) "
                f"({'alert queued' if alerted else 'no alert yet'})"
            )
        name = employer[:30] + ("" if enabled else " (disabled)")
        attempt = f"{_when(attempted)}" if attempted else "never"
        print(
            f"{name[:30]:30} {source:10} {token[:24]:24} {last or 'never':8} {attempt:21} "
            f"{'-' if pages is None else pages:>5} {_when(complete):21} {retained:>5}  {incident}"
        )
        if kind and kind != "initial" and last:
            print(f"    last attempt was a {kind} attempt")
        if last in ("failed", "partial") and error:
            print(f"    {last}: {error[:150]}")
        if hold_id:
            print(
                f"    AVAILABILITY HOLD {hold_id}: {hold_missing} of {hold_reference} jobs "
                f"missing; absences paused. Last trusted check {_when(trusted_at)}. "
                "Review with `jobmarket availability holds`."
            )
    print(f"Notifications waiting in the outbox: {pending}")
    return EXIT_OK


# Default list: fresher-friendly jobs as judged by requirement extraction (decision
# 2026-10-28). Every job is still collected and kept; the other levels are one flag away.
_LEVELS = {"default": ("yes",), "internships": ("internship",)}


def _jobs(settings: Settings, args) -> int:
    where, params = ["true"], []
    if args.board:
        where.append("j.token = %s")
        params.append(args.board)
    if args.early_career:
        where.append("j.early_career")
    if args.review:
        where.append("j.needs_review")
    level = "all" if (args.all_levels or args.early_career) else (
        "internships" if args.internships else "default")  # fmt: skip
    if level != "all":
        where.append("(c.derived ->> 'early_career') = ANY(%s)")
        params.append(list(_LEVELS[level]))
    view = "retained_jobs" if args.all_states else "active_jobs"
    source = f"{view} j LEFT JOIN current_extractions c ON c.posting_id = j.posting_id"
    with db.connect(settings) as conn:
        rows = conn.execute(
            "SELECT j.employer, j.title, j.location_text, j.texas_eligibility, j.seniority, "
            "j.early_career, j.families, j.sponsorship_status, j.needs_review, "
            "j.first_discovered_at, j.absolute_url, j.availability_state, "
            "j.qualifying_absences, j.posting_id, j.identity_basis, j.duplicate_group_id, "
            "j.open_duplicate_flags "
            f"FROM {source} WHERE {' AND '.join(where)} "
            "ORDER BY j.first_discovered_at DESC, j.employer, j.title LIMIT %s",
            (*params, args.limit),
        ).fetchall()
        total = conn.execute(f"SELECT count(*) FROM {view}").fetchone()[0]
        unchecked = conn.execute(
            f"SELECT count(*) FROM {source} WHERE c.posting_id IS NULL "
            "OR c.derived ->> 'early_career' IS NULL"
        ).fetchone()[0]
        hidden = conn.execute(
            "SELECT count(*) FROM retained_jobs WHERE NOT availability_state = ANY(%s)",
            (list(ACTIVE_STATES),),
        ).fetchone()[0]
    for (
        employer,
        title,
        location,
        eligibility,
        seniority,
        early,
        families,
        sponsorship,
        review,
        discovered,
        url,
        state,
        absences,
        posting_id,
        identity_basis,
        group_id,
        open_flags,
    ) in rows:
        flags = [eligibility, seniority]
        if identity_basis == "job_url":
            flags.append("IDENTITY FROM JOB URL")
        if group_id:
            flags.append(f"duplicate group {group_id}")
        if open_flags:
            flags.append(f"{open_flags} possible duplicate(s) to review")
        if early:
            flags.append("early-career")
        if review:
            flags.append("NEEDS REVIEW")
        if sponsorship != "not_stated":
            flags.append(f"sponsorship: {sponsorship}")
        print(f"{employer}: {title}  [posting {posting_id}]")
        print(f"    {location or 'no location'} | {', '.join(flags)} | {', '.join(families)}")
        print(f"    discovered {_when(discovered)} | {url}")
        if state == "possibly_unavailable":
            print(
                f"    WARNING: possibly unavailable (missing from {absences} qualifying "
                "scan(s)); it may still be open"
            )
        elif state != "observed":
            print(f"    {state.replace('_', ' ')} (hidden from the active feed)")
    shown = "retained" if args.all_states else "active"
    what = {"default": "fresher-friendly ", "internships": "internship ", "all": ""}[level]
    print(f"\nShowing {len(rows)} {what}job(s) of {total} {shown} job(s).")
    if level != "all":
        print(
            f"{unchecked} {shown} job(s) have no early-career answer yet (not extracted, or "
            "extracted before that field existed) and are not shown; use --all-levels."
        )
    if not args.all_states and hidden:
        print(f"{hidden} retained job(s) are assumed inactive or closed; use --all-states.")
    return EXIT_OK


def _duplicates(settings: Settings, args) -> int:
    cmd = args.duplicates_command
    if cmd != "list":
        try:
            with db.connect(settings, autocommit=True) as conn:
                if cmd == "confirm":
                    group = confirm_flag(conn, args.id, args.reason, system_clock())
                    print(f"Flag {args.id} confirmed; records grouped as group {group}.")
                elif cmd == "dismiss":
                    dismiss_flag(conn, args.id, args.reason, system_clock())
                    print(f"Flag {args.id} dismissed; the rule will not raise it again.")
                else:
                    dissolve_group(conn, args.id, args.reason, system_clock())
                    print(f"Group {args.id} dissolved; its records stay separate.")
        except ReviewError as exc:
            print(f"Not done: {exc}.", file=sys.stderr)
            return EXIT_FAILURE
        return EXIT_OK

    with db.connect(settings) as conn:
        groups = conn.execute(
            "SELECT g.group_id, g.rule, g.rule_version, g.confidence, g.status, g.created_at, "
            "g.dissolved_reason, array_agg(m.posting_id ORDER BY m.posting_id) "
            "FROM duplicate_groups g JOIN duplicate_group_members m USING (group_id) "
            + ("" if args.all else "WHERE g.status = 'active' ")
            + "GROUP BY g.group_id ORDER BY g.group_id"
        ).fetchall()
        flags = conn.execute(
            "SELECT f.flag_id, f.posting_id, f.related_posting_id, f.rule, f.rule_version, "
            "f.status, f.evidence, f.review_reason, va.title, ea.name, vb.title "
            "FROM posting_duplicate_flags f "
            "JOIN source_postings pa ON pa.posting_id = f.posting_id "
            "JOIN posting_versions va ON va.version_id = pa.current_version_id "
            "JOIN source_boards ba ON ba.board_id = pa.board_id "
            "JOIN employers ea ON ea.employer_id = ba.employer_id "
            "JOIN source_postings pb ON pb.posting_id = f.related_posting_id "
            "JOIN posting_versions vb ON vb.version_id = pb.current_version_id "
            + ("" if args.all else "WHERE f.status = 'open' ")
            + "ORDER BY f.flag_id"
        ).fetchall()
    print(f"Duplicate groups ({'all' if args.all else 'active'}): {len(groups)}")
    for gid, rule, version, confidence, status, created, why, members in groups:
        print(
            f"  group {gid} [{status}] {rule} ({confidence}, rules {version}), created "
            f"{_when(created)}: postings {', '.join(map(str, members))}"
        )
        if why:
            print(f"      dissolved: {why}")
    print(f"Possible duplicates ({'all' if args.all else 'open'}): {len(flags)}")
    for fid, a, b, rule, version, status, evidence, review, title_a, employer, title_b in flags:
        print(f"  flag {fid} [{status}] {employer}: postings {a} and {b} ({rule}, {version})")
        print(f"      {title_a!r} / {title_b!r}")
        for note in (evidence or {}).get("conflicts") or []:
            print(f"      conflict: {note}")
        if review:
            print(f"      review: {review}")
    return EXIT_OK


def _availability(settings: Settings, args) -> int:
    cmd = args.availability_command
    try:
        if cmd == "close":
            with db.connect(settings, autocommit=True) as conn:
                before = close_posting(
                    conn, args.posting, args.reason, args.evidence_url, args.evidence_text,
                    system_clock(),
                )  # fmt: skip
            print(f"Posting {args.posting}: {before} -> closed. Evidence recorded.")
            return EXIT_OK
        if cmd == "release":
            with db.connect(settings, autocommit=True) as conn:
                new_ref = release_hold(
                    conn, args.hold, args.reason.replace("-", "_"), args.evidence, system_clock()
                )
            print(f"Hold {args.hold} released ({args.reason}).")
            print(
                f"Scan {new_ref} is the board's new reference; "
                if new_ref
                else "The board keeps its last trusted reference; "
            )
            print("absence counting restarts with the next qualifying scan.")
            return EXIT_OK
    except AvailabilityError as exc:
        print(f"Not done: {exc}.", file=sys.stderr)
        return EXIT_FAILURE

    with db.connect(settings) as conn:
        if cmd == "holds":
            rows = conn.execute(
                "SELECT h.hold_id, e.name, b.source_system, b.board_key, h.status, h.trigger, "
                "h.missing_count, h.reference_count, h.opened_at, h.opened_by_scan_id, "
                "h.reference_scan_id, h.released_at, h.release_reason, h.release_evidence, "
                "(SELECT count(*) FROM absence_checks c WHERE c.hold_id = h.hold_id) AS scans "
                "FROM board_holds h JOIN source_boards b USING (board_id) "
                "JOIN employers e USING (employer_id) "
                + ("" if args.all else "WHERE h.status = 'active' ")
                + "ORDER BY h.hold_id"
            ).fetchall()
            if not rows:
                print("No active board holds." if not args.all else "No board holds.")
            for (
                hid, employer, source, token, status, trigger, missing, ref, opened,
                by_scan, ref_scan, released, reason, evidence, scans,
            ) in rows:  # fmt: skip
                print(
                    f"Hold {hid} [{status}] {employer} ({source} {token}): {missing} of {ref} "
                    f"reference jobs missing ({trigger})"
                )
                print(
                    f"    opened {_when(opened)} by scan {by_scan}; reference scan {ref_scan}; "
                    f"{scans} scan(s) judged under this hold"
                )
                if released:
                    print(f"    released {_when(released)}: {reason}; evidence: {evidence}")
            return EXIT_OK

        # show
        posting = conn.execute(
            "SELECT p.posting_id, e.name, b.board_key, p.source_posting_id, "
            "p.availability_state, p.qualifying_absences, p.first_discovered_at, "
            "p.last_observed_at FROM source_postings p JOIN source_boards b USING (board_id) "
            "JOIN employers e USING (employer_id) WHERE p.posting_id = %s",
            (args.posting,),
        ).fetchone()
        if posting is None:
            print("No such posting.", file=sys.stderr)
            return EXIT_FAILURE
        pid, employer, token, sid, state, absences, first, last = posting
        print(f"Posting {pid}: {employer}, board {token}, job {sid}")
        print(
            f"  state: {state} ({absences} qualifying absence(s)); first discovered "
            f"{_when(first)}, last observed {_when(last)}"
        )
        for occurred, frm, to, _n, reason, scan, evidence in conn.execute(
            "SELECT occurred_at, from_state, to_state, qualifying_absences, reason, "
            "board_scan_id, evidence FROM posting_availability_events WHERE posting_id = %s "
            "ORDER BY event_id",
            (pid,),
        ):
            where = f" (scan {scan})" if scan else ""
            print(f"  {_when(occurred)}  {frm or '-'} -> {to}{where}: {reason}")
            if evidence and evidence.get("url"):
                print(f"      evidence: {evidence.get('url')} | {evidence.get('text')}")
    return EXIT_OK


def _notifications(settings: Settings, args) -> int:
    with db.connect(settings, autocommit=True) as conn:
        if args.send_test:
            from jobmarket.operations import queue_notification

            now = system_clock()
            queue_notification(conn, "alert", "[Job Pipeline] Test email",
                               "This is a test of the server's email delivery. Nothing is "
                               "wrong.", now, dedup_key=f"test:{now.isoformat()}")  # fmt: skip
            args.send = True
        if args.send:
            result = send_pending(conn, smtp_settings(), system_clock())
            if not result.configured:
                print(
                    f"Email delivery is not configured (no JOBMARKET_SMTP_HOST / "
                    f"JOBMARKET_ALERT_FROM / JOBMARKET_ALERT_TO). {result.pending_left} "
                    "notification(s) stay pending in the outbox."
                )
                return EXIT_OK
            print(f"Sent {result.sent}, failed {result.failed}, pending {result.pending_left}.")
            return EXIT_OK if result.failed == 0 else EXIT_FAILURE
        rows = conn.execute(
            "SELECT notification_id, kind, status, created_at, subject, body FROM notifications "
            "ORDER BY notification_id DESC LIMIT %s",
            (args.limit,),
        ).fetchall()
    if not rows:
        print("The outbox is empty.")
    for nid, kind, status, created, subject, body in rows:
        print(f"#{nid} [{kind}, {status}] {_when(created)}: {subject}")
        if args.full:
            print("    " + body.replace("\n", "\n    "))
    return EXIT_OK


def _cleanup_raw(settings: Settings, args) -> int:
    from jobmarket.s3store import BUCKET_ENV, S3Unavailable, raw_upload_confirmation

    started = system_clock()
    confirmed = None
    if args.require_s3 or os.environ.get(BUCKET_ENV, "").strip():
        # On a server raw files leave the disk only after S3 confirms them.
        try:
            confirmed = raw_upload_confirmation(_s3_store())
        except (S3Unavailable, ConfigError) as exc:
            print(f"Not cleaned up: cannot confirm uploads ({exc}).", file=sys.stderr)
            if not args.dry_run:
                _record(settings, "raw_cleanup", started, "failed",
                        error=f"cannot confirm uploads: {exc}")  # fmt: skip
            return EXIT_CONFIG_ERROR if isinstance(exc, ConfigError) else EXIT_FAILURE
    with db.connect(settings, autocommit=True) as conn:
        result = cleanup_raw(conn, raw_dir(), system_clock(), RAW_RETENTION, args.dry_run,
                             confirmed)  # fmt: skip
    verb = "Would delete" if result.dry_run else "Deleted"
    print(
        f"{verb} {result.files_deleted} raw response file(s) older than "
        f"{RAW_RETENTION.days} days ({result.files_already_missing} already missing; "
        f"{result.purged_rows} row(s) {'to mark' if result.dry_run else 'marked'} purged) "
        f"and {result.orphan_files_deleted} orphan file(s). Job records are not touched."
    )
    if confirmed is not None:
        print(f"Kept {result.kept_not_uploaded} old file(s) not yet confirmed in S3.")
    if not args.dry_run:
        _record(settings, "raw_cleanup", started, "succeeded", {
            "deleted": result.files_deleted + result.orphan_files_deleted,
            "kept_not_uploaded": result.kept_not_uploaded,
            "s3_confirmed": confirmed is not None})  # fmt: skip
    return EXIT_OK


def _replay(settings: Settings, args) -> int:
    with db.connect(settings, autocommit=True) as conn:
        scan_ids = available_scan_ids(conn) if args.available else args.scan
        try:
            results = replay_scans(conn, raw_dir(), scan_ids, system_clock)
        except CollectionBusy as exc:
            print(f"Not started: {exc}.", file=sys.stderr)
            return EXIT_BUSY
        except UnknownScan as exc:
            print(f"Not started: {exc}.", file=sys.stderr)
            return EXIT_FAILURE
    if not results:
        print("No saved raw responses are available to replay.")
    for r in results:
        print(f"Scan {r.board_scan_id} ({r.token}, collected {_when(r.collected_at)}): {r.outcome}")
        if r.counts:
            c = r.counts
            print(
                f"    validation {r.validation}; postings new {c.new_postings}, versions new "
                f"{c.new_versions}, observations new {c.new_observations} "
                f"(changed {c.changed}, unchanged {c.unchanged}, of which cosmetic only "
                f"{c.cosmetic_only}; older than current {c.historical}); already recorded "
                f"{c.already_recorded}"
            )
            if r.scan_status != "complete":
                print(
                    f"    The scan was recorded as {r.scan_status}; replay adds history but does "
                    "not change the scan's status, retries, or absence evidence."
                )
        if r.detail:
            print(f"    {r.detail}")
    outcomes = [r.outcome for r in results]
    if results:
        tally = ", ".join(f"{outcomes.count(o)} {o}" for o in dict.fromkeys(outcomes))
        print(f"Replayed {len(results)} scan(s): {tally}.")
    bad = {"failed", "checksum_mismatch", "file_missing"}
    return EXIT_FAILURE if bad & set(outcomes) else EXIT_OK


def _history(settings: Settings, args) -> int:
    with db.connect(settings) as conn:
        if args.posting is not None:
            where, params = "p.posting_id = %s", (args.posting,)
        elif args.board and args.job:
            where, params = "b.board_key = %s AND p.source_posting_id = %s", (args.board, args.job)
        else:
            print("Give a posting id, or --board TOKEN --job SOURCE_ID.", file=sys.stderr)
            return EXIT_FAILURE
        posting = conn.execute(
            "SELECT p.posting_id, e.name, b.board_key, p.source_posting_id, "
            "p.first_discovered_at, p.last_observed_at, cv.version_no, co.observed_at, cv.title "
            "FROM source_postings p JOIN source_boards b ON b.board_id = p.board_id "
            "JOIN employers e ON e.employer_id = b.employer_id "
            "LEFT JOIN posting_versions cv ON cv.version_id = p.current_version_id "
            "LEFT JOIN posting_observations co ON co.observation_id = p.current_observation_id "
            f"WHERE {where}",
            params,
        ).fetchone()
        if posting is None:
            print("No such posting.", file=sys.stderr)
            return EXIT_FAILURE
        posting_id, employer, token, source_id, first, last, cur_no, cur_at, cur_title = posting
        by_kind = dict(
            conn.execute(
                "SELECT recorded_by, count(*) FROM posting_observations WHERE posting_id = %s "
                "GROUP BY recorded_by",
                (posting_id,),
            ).fetchall()
        )
        stages = conn.execute(
            "SELECT stage_no, version_no, first_observed_at, last_observed_at, observations, "
            "change_window_start, change_window_end, changed_fields "
            "FROM posting_version_stages WHERE posting_id = %s ORDER BY stage_no",
            (posting_id,),
        ).fetchall()
        versions = conn.execute(
            "SELECT version_no, detection, normalization_version, first_seen_at, recorded_at, "
            "title FROM posting_versions WHERE posting_id = %s ORDER BY version_no",
            (posting_id,),
        ).fetchall()

    print(f"Posting {posting_id}: {employer}, board {token}, job {source_id}")
    observations = ", ".join(f"{n} {kind}" for kind, n in sorted(by_kind.items())) or "none"
    print(f"  first discovered {_when(first)}, last observed {_when(last)}")
    print(f"  observations: {observations}")
    if cur_no is None:
        print(
            "  No content history yet: first seen before content was kept for every listing; "
            "it is recorded the next time it is observed."
        )
        return EXIT_OK
    print(f"  current: v{cur_no} {cur_title!r}, observed {_when(cur_at)}")
    print("Stages, in collection order (a change happened inside its window):")
    for no, v_no, s_first, s_last, n, w_start, w_end, fields in stages:
        line = f"  {no:>3}  v{v_no:<3} {_when(s_first)} to {_when(s_last)}  ({n} obs)"
        if w_start is not None:
            changed = ", ".join(fields) if fields else "fields not compared yet"
            line += f"  changed after {_when(w_start)}, by {_when(w_end)}: {changed}"
        print(line)
    print("Versions, in recording order:")
    for v_no, detection, rules, seen, recorded, title in versions:
        print(
            f"  v{v_no:<3} {detection:15} rules {rules or 'none (exact content)':22} "
            f"first seen {_when(seen)}, recorded {_when(recorded)}  {title!r}"
        )
    return EXIT_OK


def _listing(settings: Settings, args) -> int:
    with db.connect(settings) as conn:
        detail = listing_detail(conn, args.posting)
    if detail is None:
        print("No stored content for that posting.", file=sys.stderr)
        return EXIT_FAILURE
    d = detail.description
    print(f"{detail.title} - {detail.employer} ({detail.source_system}:{detail.token})")
    print(f"  locations: {'; '.join(detail.locations) or 'not stated'}")
    print(f"  posting page: {detail.posting_url or 'none'}")
    if detail.apply_url:
        print(f"  application page: {detail.apply_url}")
    print(
        f"  availability: {detail.availability_state.replace('_', ' ')}"
        f" (missing from {detail.qualifying_absences} qualifying scan(s)); last observed "
        f"{_when(detail.last_observed_at)}; last trusted board check "
        f"{_when(detail.last_trusted_check_at)}"
    )
    print(
        f"  first discovered {_when(detail.first_discovered_at)}; source published "
        f"{_when(detail.source_published_at) if detail.source_published_at else 'not given'}; "
        f"this content seen since "
        f"{_when(detail.content_seen_at)} (version {detail.version_no})"
    )
    print(f"  kept by screening: {'yes' if detail.retained else 'no'}")
    print(f"  {d.source_note}")
    if d.status == "omitted_too_long":
        print(f"  Description not stored ({d.length} characters); see the posting page.")
    elif d.status == "empty":
        print("  The source supplied no description text.")
    else:
        print()
        print(d.safe_html if args.html else plain_text(d.safe_html))
    return EXIT_OK


def _extract(settings: Settings, args) -> int:
    try:
        config = extraction.ExtractionConfig.from_env()
    except extraction.ExtractionNotConfigured as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    command = args.extract_command
    if command == "run":
        return _extract_run(settings, config, args)
    if command == "worker":
        return _extract_worker(settings, config)
    with db.connect(settings, autocommit=True) as conn:
        if command == "status":
            now = system_clock()
            _extract_status(extraction.status(conn, config, now))
            _extract_pipeline(conn, config, now)
        elif command == "show":
            return _extract_show(conn, args)
        elif command == "reprocess":
            try:
                result = extraction.reprocess(
                    conn, config, system_clock(), args.reason, args.limit, args.retained_only
                )
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return EXIT_FAILURE
            print(
                f"Queued {result.queued} listing(s) under {config.key}; reset {result.reset} "
                f"that had given up; superseded {result.superseded} open job(s) under other "
                "configurations. Nothing is sent until `jobmarket extract run`."
            )
        elif command == "renormalize":
            n = extraction.renormalize(conn, system_clock())
            print(f"Re-normalized {n} stored answer(s); no model calls were made.")
        elif command == "disagreements":
            rows = conn.execute(
                "SELECT posting_id, employer, title, rules_retained, ai_would_retain, "
                "rule_families, ai_categories, rule_texas_eligibility, ai_place "
                "FROM screening_disagreements ORDER BY rules_retained DESC, posting_id LIMIT %s",
                (args.limit,),
            ).fetchall()
            total = conn.execute("SELECT count(*) FROM screening_disagreements").fetchone()[0]
            print(f"{total} listing(s) where the AI and the rules disagree (rules decide):")
            for pid, emp, title, kept, ai, fams, cats, elig, place in rows:
                print(
                    f"  {pid:>6}  rules {'keep' if kept else 'skip'} / AI "
                    f"{'keep' if ai else 'skip'}  {emp}: {title}\n"
                    f"          rules {list(fams or [])} {elig}; AI {cats} {place}"
                )
    return EXIT_OK


def _extract_run(settings: Settings, config, args) -> int:
    import os

    from jobmarket.gemini import API_KEY_ENV, GeminiClient

    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        print(f"Configuration error: {API_KEY_ENV} is not set.", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    try:
        config.require_limits()
    except extraction.ExtractionNotConfigured as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    with db.connect(settings, autocommit=True) as conn:
        if args.skip_if_worker and extraction_worker.worker_running(conn):
            print("The extraction worker is running and handles the queue; nothing to do.")
            return EXIT_BUSY
        client = GeminiClient(key, config.model, config.thinking_level, config.timeout_seconds)
        report = extraction.run_extraction(
            conn, client.generate, config, system_clock, time.sleep, args.limit
        )
    print(
        f"Extraction run {report.run_id} ({config.key}): {report.requests} request(s), "
        f"{report.succeeded} extracted ({report.warnings} with warnings), {report.reused} "
        f"reused without a request, {report.failed} failed ({report.gave_up} gave up), "
        f"{report.superseded} superseded by newer content."
    )
    if report.retries_scheduled:
        print(f"{report.retries_scheduled} listing(s) will be retried shortly (invalid answer).")
    if report.recovered:
        print(f"Recovered {report.recovered} listing(s) left running by a process that ended.")
    if report.queued_now:
        print(f"Queued {report.queued_now} newly stored listing(s) first.")
    print(f"Stopped: {report.stop_reason}.")
    for error in report.errors[:10]:
        print(f"  {error}")
    return EXIT_OK


def _extract_worker(settings: Settings, config) -> int:
    import os

    from jobmarket.gemini import API_KEY_ENV, GeminiClient

    stopper = extraction_worker.Stopper()
    stopper.install()
    worker = extraction_worker.worker_id()
    problem = None
    key = os.environ.get(API_KEY_ENV, "").strip()
    try:
        if not key:
            raise extraction.ExtractionNotConfigured(f"{API_KEY_ENV} is not set")
        config.require_limits()
        client = GeminiClient(key, config.model, config.thinking_level, config.timeout_seconds)
    except extraction.ExtractionNotConfigured as exc:
        problem = str(exc)
    with db.connect(settings, autocommit=True) as conn:
        if problem is not None:
            print(f"Configuration error: {problem}. Waiting to be stopped.", file=sys.stderr,
                  flush=True)  # fmt: skip
            extraction_worker.run_unconfigured(conn, worker, system_clock, stopper, problem)
            return EXIT_CONFIG_ERROR
        try:
            extraction_worker.run_worker(
                conn, client.generate, config, system_clock, stopper, worker,
                log=lambda line: print(line, flush=True),
            )  # fmt: skip
        except extraction.ExtractionBusy as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_BUSY
    return EXIT_OK


def _minutes(value) -> str:
    return "-" if value is None else f"{value:.1f} min"


def _extract_pipeline(conn, config, now) -> None:
    """Worker state, queue depth and age, throughput, and latency (see queue_metrics)."""
    labels = {1: "new retained", 2: "retained backlog", 3: "new other", 4: "other backlog"}
    alive = extraction_worker.worker_running(conn)
    latest = extraction_worker.latest_worker(conn)
    if latest is None:
        print("Worker: none has run yet (the hourly batch handles the queue).")
    else:
        name, state, detail, beat, _ = latest
        status = state if alive or state == "stopped" else f"{state}, process gone"
        print(f"Worker {name}: {status}; {detail or ''} (last heartbeat {_when(beat)}).")
    info = extraction.queue_metrics(conn, config, now)
    print("Open jobs: ready / waiting for a retry, oldest queued")
    for priority, ready, waiting, oldest in info["depth"]:
        age = (now - oldest).total_seconds() / 3600
        print(f"  {labels[priority]:17} {ready:>6} / {waiting:<4} {_when(oldest)} ({age:.1f} h)")
    print(f"Extracted in the last hour: {info['extracted_last_hour']}.")
    print("Last 24 hours, by lane: extracted, reused; queued to result p50/p95; first "
          "collected to result p50/p95")  # fmt: skip
    for t in info["timing"]:
        print(
            f"  {t.lane:8} {t.extracted:>5}, {t.reused:>4} reused; "
            f"{_minutes(t.queue_p50)} / {_minutes(t.queue_p95)}; "
            f"{_minutes(t.collection_p50)} / {_minutes(t.collection_p95)}"
        )
    if info["outcomes_today"]:
        print("Requests today by outcome: " + ", ".join(
            f"{outcome} {n}" for outcome, n in info["outcomes_today"]))  # fmt: skip


def _extract_status(info: dict) -> None:
    print(f"Active configuration: {info['config_key']}")
    labels = {1: "new retained", 2: "retained backlog", 3: "new other", 4: "other backlog"}
    if not info["queue"]:
        print("  queue: empty")
    for state, priority, n in info["queue"]:
        print(f"  {state:10} {labels[priority]:17} {n:>6}")
    if info["other_config_open"]:
        print(
            f"  {info['other_config_open']} open job(s) under other configurations "
            "(not run; `extract reprocess` supersedes them)"
        )
    if info["not_yet_queued"]:
        print(f"  {info['not_yet_queued']} listing(s) not queued yet (queued by the next "
              "collection or run)")  # fmt: skip
    req, ok, prompt, out, thought, total = info["usage_today"]
    print(
        f"Today ({info['pacific_day']}, Pacific): {req} request(s), {ok} successful; tokens "
        f"{prompt} in, {out} out, {thought} thinking, {total} total."
    )
    if info["paid_equivalent_today"] is not None:
        print(
            f"  At paid rates this would be about ${info['paid_equivalent_today']:.4f}; the "
            "free tier is not billed (planning figure only)."
        )
    if info["safety_stop_at"] is not None:
        print(
            f"  Safety stop at {info['safety_stop_at']} request(s) today. Backlog lane: "
            f"{info['backlog_today']} of {info['backlog_allowance']} used; "
            f"{info['new_reserve']} kept for new listings."
        )
    if info["paused_until"]:
        print(f"  Paused until {_when(info['paused_until'])} (daily quota).")
    results = info["current_results"]
    print(
        f"Current results: {results.get('valid', 0)} valid, "
        f"{results.get('valid_with_warnings', 0)} with warnings."
    )


def _extract_show(conn, args) -> int:
    row = conn.execute(
        "SELECT c.result_id, c.config_key, c.validation_status, c.normalized, c.derived, "
        "r.validation_issues, r.evidence, r.created_at FROM current_extractions c "
        "JOIN extraction_results r USING (result_id) WHERE c.posting_id = %s",
        (args.posting,),
    ).fetchone()
    if row is None:
        job = conn.execute(
            "SELECT j.status, j.skip_reason, j.last_error FROM extraction_jobs j "
            "JOIN source_postings p ON p.current_version_id = j.version_id "
            "WHERE p.posting_id = %s ORDER BY j.job_id DESC LIMIT 1",
            (args.posting,),
        ).fetchone()
        if job is None:
            print("Not queued for extraction yet.")
        else:
            print(f"No result for the current content: {job[0]} {job[1] or ''} {job[2] or ''}")
        return EXIT_OK
    result_id, key, valid, norm, derived, issues, evidence, created = row
    print(f"Result {result_id} ({key}), {valid}, extracted {_when(created)}")
    for line in summary_lines(norm):
        print(f"  {line}")
    verdict = "would keep" if derived["ai_would_retain"] else "would skip"
    print(
        f"  compared with screening: AI {verdict}"
        f" (categories {derived['role_categories']}, place {derived['place']})"
    )
    for issue in issues:
        print(f"  warning: {issue['path']}: {issue['issue']} {issue.get('detail') or ''}")
    if args.evidence:
        for item in evidence:
            mark = "found" if item["found"] else "NOT FOUND"
            print(f"  [{mark}] {item['path']}: {item['quote']!r}")
    return EXIT_OK


def _eval(settings: Settings, args) -> int:
    import json

    from jobmarket import evaluation

    directory = evaluation.eval_dir()
    try:
        config = extraction.ExtractionConfig.from_env()
    except extraction.ExtractionNotConfigured as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    if args.eval_command == "prepare":
        selection = json.loads(args.selection.read_text())["cases"]
        with db.connect(settings, autocommit=True) as conn:
            written = evaluation.prepare(conn, directory, selection)
        print(
            f"Wrote {len(written)} case file(s) under {directory / 'cases'} (existing files kept)."
        )
        return EXIT_OK
    try:
        cases = evaluation.load_cases(directory)
    except (ValueError, OSError) as exc:
        print(f"Evaluation cases: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    if args.eval_command == "run":
        import os

        from jobmarket.gemini import API_KEY_ENV, GeminiClient

        key = os.environ.get(API_KEY_ENV, "").strip()
        try:
            config.require_limits()
            if not key:
                raise extraction.ExtractionNotConfigured(f"{API_KEY_ENV} is not set")
        except extraction.ExtractionNotConfigured as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return EXIT_CONFIG_ERROR
        client = GeminiClient(key, config.model, config.thinking_level, config.timeout_seconds)
        with db.connect(settings, autocommit=True) as conn:
            try:
                report = evaluation.run_cases(
                    conn, client.generate, config, system_clock, time.sleep, cases,
                    args.split, args.final,
                )  # fmt: skip
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return EXIT_FAILURE
        if report is None:
            print("Every case in this split already has a result under the active configuration.")
        else:
            print(f"{report.requests} request(s), {report.succeeded} extracted, "
                  f"{report.reused} reused, {report.failed} failed; stopped: "
                  f"{report.stop_reason}.")  # fmt: skip
        return EXIT_OK
    with db.connect(settings) as conn:
        try:
            comparisons = evaluation.compare(conn, config, cases, args.split, args.final)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_FAILURE
    _print_eval(evaluation, comparisons, args, config)
    return EXIT_OK


def _print_eval(evaluation, comparisons, args, config) -> None:
    summary = evaluation.summarize(comparisons)
    print(f"Split {args.split}, configuration {config.key}")
    for state, title in (
        ("human_reviewed", "Human-reviewed labels (accuracy)"),
        ("proposed", "Proposed labels, NOT human-reviewed (agreement only)"),
    ):
        part = summary[state]
        if not part["cases"]:
            continue
        print(f"{title}: {part['cases']} case(s)")
        for name, entry in part["fields"].items():
            line = f"  {name:34} {entry['agree']}/{entry['cases']} cases match"
            if "tp" in entry:
                line += f"  (matched {entry['tp']}, extra {entry['fp']}, missed {entry['fn']})"
            print(line)
        print(f"  quotes not found in the posting: {part['quotes_not_found']}; claims without "
              f"evidence: {part['claims_without_evidence']}")  # fmt: skip
    if summary["missing_results"]:
        print(f"No result yet for: {', '.join(summary['missing_results'])}")
    tokens = [(c.prompt_tokens, c.output_tokens) for c in comparisons if c.prompt_tokens]
    if tokens:
        print(f"Usage per listing: {sum(t[0] for t in tokens) / len(tokens):.0f} input and "
              f"{sum(t[1] for t in tokens) / len(tokens):.0f} output tokens on average "
              f"({len(tokens)} listing(s))")  # fmt: skip
    if args.cases:
        for c in comparisons:
            if c.result_id is None:
                continue
            print(f"{c.case.case_id} ({c.case.review}, posting {c.case.posting_id}):")
            for name, (label, model) in c.mismatches.items():
                print(f"  {name}: label {_fmt(label)} / model {_fmt(model)}")


def _fmt(value) -> str:
    if isinstance(value, (set, frozenset)):
        return "{" + ", ".join(sorted(map(_fmt, value))) + "}"
    if isinstance(value, tuple):
        return "(" + ", ".join(map(_fmt, value)) + ")"
    return str(value)


def _read_new_password(prompt=None) -> str | None:
    """A new password typed twice at a hidden prompt (never echoed, logged, or printed)."""
    import getpass

    prompt = prompt or getpass.getpass
    first = prompt("New password: ")
    if first != prompt("Repeat the password: "):
        print("The two passwords differ; nothing was changed.", file=sys.stderr)
        return None
    return first


def _owner(settings: Settings, args, prompt=None) -> int:
    try:
        from jobmarket import owner
    except ImportError:
        print('Owner accounts need argon2-cffi: pip install -e ".[web]"', file=sys.stderr)
        return EXIT_CONFIG_ERROR
    from jobmarket.migrate import current_private_schema

    now = system_clock()
    with db.connect(settings, autocommit=True) as conn:
        schema = current_private_schema(conn)
        try:
            if args.owner_command == "status":
                info = owner.owner_status(conn, schema, now)
                if not info["exists"]:
                    print("No owner account yet. Create it with `jobmarket owner create`.")
                    return EXIT_OK
                print(f"Owner account: {info['username']} (created {_when(info['created_at'])}, "
                      f"password changed {_when(info['password_changed_at'])})")  # fmt: skip
                print(f"Open sessions: {info['open_sessions']}")
                if info["paused_until"]:
                    print(f"Sign-in is paused until {_when(info['paused_until'])} "
                          "(3 wrong passwords in a row).")  # fmt: skip
                return EXIT_OK
            if args.owner_command == "sign-out-all":
                print(f"Ended {owner.end_all_sessions(conn, schema)} session(s).")
                return EXIT_OK
            password = _read_new_password(prompt)
            if password is None:
                return EXIT_FAILURE
            if args.owner_command == "create":
                owner.create_owner(conn, schema, args.username, password, now)
                print(f"Created the owner account {args.username}. Sign in at /signin.")
            else:
                ended = owner.reset_password(conn, schema, password, now)
                print(f"Password changed; {ended} session(s) ended and any sign-in pause "
                      "cleared.")  # fmt: skip
            return EXIT_OK
        except owner.OwnerError as exc:
            print(f"Not done: {exc}.", file=sys.stderr)
            return EXIT_FAILURE


def _private(settings: Settings, args) -> int:
    from jobmarket import private_db

    with db.connect(settings, autocommit=True) as conn:
        if args.private_command == "grant":
            try:
                for statement in private_db.grant_private_access(conn):
                    print(f"  {statement}")
            except private_db.PrivateLoginMissing as exc:
                print(f"Not done: {exc}.", file=sys.stderr)
                return EXIT_FAILURE
            print(f"{private_db.PRIVATE_ROLE} can now use the private schema, and only it.")
            return EXIT_OK
        report = private_db.access_report(conn)
    ok = True
    for check, value in report.items():
        expected = private_db.EXPECTED[check]
        if value is None:
            state, good = "login does not exist", False
        else:
            state, good = ("yes" if value else "no"), value == expected
        ok = ok and good
        print(f"  {check}: {state}{'' if good else '  <-- not as expected'}")
    return EXIT_OK if ok else EXIT_FAILURE


def _web(settings: Settings, args) -> int:
    try:
        from jobmarket.web import create_app
        from jobmarket.web.config import load_web_config
    except ImportError:
        print('The website needs Flask: pip install -e ".[web]"', file=sys.stderr)
        return EXIT_CONFIG_ERROR
    from jobmarket.search import ServingUnavailable, site_status

    config = load_web_config()
    # Fail before serving if the analytics login or the serving views are not ready.
    try:
        with db.connect(config.settings) as conn:
            conn.read_only = True
            site_status(conn, config.clock(), config.serving_schema)
    except ServingUnavailable as exc:
        print(f"Cannot start the website: {exc}.", file=sys.stderr)
        return EXIT_FAILURE
    except psycopg.Error as exc:
        print(f"Cannot start the website as {config.settings.db_user}: {exc}".rstrip(),
              file=sys.stderr)  # fmt: skip
        print("Check ANALYTICS_DB_PASSWORD in .env and run `jobmarket analytics build`.",
              file=sys.stderr)  # fmt: skip
        return EXIT_FAILURE
    print(f"Job search on http://{args.host}:{args.port}/ (Ctrl+C stops it). This is "
          "Flask's local development server, for use on this computer.")  # fmt: skip
    create_app(config).run(host=args.host, port=args.port, debug=False, use_reloader=False)
    return EXIT_OK


def _analytics(settings: Settings, args) -> int:
    from jobmarket import analytics

    command = args.analytics_command
    if command in ("grant", "check-access"):
        with db.connect(settings, autocommit=True) as conn:
            try:
                if command == "grant":
                    for statement in analytics.grant_read_access(conn):
                        print(f"  {statement}")
                    print(f"{analytics.ANALYTICS_ROLE} can now read ingestion tables and "
                          "create its own analytics schemas.")  # fmt: skip
                    return EXIT_OK
                checks = analytics.read_only_check(conn)
            except analytics.AnalyticsUnavailable as exc:
                print(str(exc), file=sys.stderr)
                return EXIT_CONFIG_ERROR
        for name, allowed in checks.items():
            print(f"  {name:28} {'yes' if allowed else 'no'}")
        writes = [k for k, v in checks.items() if v and k != "SELECT"]
        if writes or not checks["SELECT"]:
            print("Not read-only as intended.", file=sys.stderr)
            return EXIT_FAILURE
        print("Read-only on ingestion tables, as intended.")
        return EXIT_OK
    if command == "publish":
        return _analytics_publish(settings, args)
    dbt_args = {"build": ["build"], "test": ["test"], "docs": ["docs", "generate"]}[command]
    if command == "build" and args.select:
        # Run only tests whose parents are all selected, so a partial build does not test
        # models it did not build.
        dbt_args += ["--select", args.select, "--indirect-selection", "cautious"]
    target = getattr(args, "target", "analytics")
    try:
        outcome = analytics.run_dbt(dbt_args, target=target)
    except analytics.AnalyticsUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG_ERROR
    for failure in outcome.failures:
        print(f"  {failure}", file=sys.stderr)
    if command == "docs" and outcome.success:
        print(f"Documentation written to {analytics.PROJECT_DIR / 'target'} "
              "(open with: cd analytics && dbt docs serve --profiles-dir .)")  # fmt: skip
    return EXIT_OK if outcome.success else EXIT_FAILURE


def _s3_store():
    from jobmarket.s3store import S3Store, load_s3_config

    return S3Store(load_s3_config())


def _record(settings: Settings, step, started, status, detail=None, error=None) -> None:
    """Record a pipeline step; a failure to record never hides the step's own result."""
    from jobmarket import operations

    try:
        with db.connect(settings, autocommit=True) as conn:
            operations.record_step(conn, step, started, system_clock(), status, detail, error)
    except psycopg.Error as exc:
        print(f"Could not record the {step} step: {exc}".rstrip(), file=sys.stderr)


def _backup(settings: Settings, args) -> int:
    from dataclasses import replace

    from jobmarket import backup
    from jobmarket.s3store import S3Unavailable

    started = system_clock()
    command = args.backup_command
    try:
        if command == "create":
            directory = backup.create_backup(settings, Path(args.dir), started)
            manifest = backup.verify_backup(directory)
            for part, info in manifest["parts"].items():
                print(f"  {part:9} {info['file']:15} {info['bytes']:>12,} bytes")
            print(f"Backup written to {directory} ({manifest['migrations']['applied']} "
                  "migrations applied).")  # fmt: skip
            size = sum(info["bytes"] for info in manifest["parts"].values())
            detail = {"stamp": manifest["stamp"], "bytes": size, "uploaded": False}
            if args.upload:
                keys = backup.upload_backup(_s3_store(), directory, system_clock())
                detail["uploaded"] = True
                print(f"Uploaded {len(keys)} files to S3 (encrypted with S3-managed keys) "
                      "and confirmed them.")  # fmt: skip
            _record(settings, "backup", started, "succeeded", detail)
        elif command == "prune":
            result = backup.prune_backups(Path(args.dir), args.keep)
            print(f"Kept {len(result.kept)} newest verified backup(s): {', '.join(result.kept)}")
            for label, names in (("Deleted (confirmed in S3)", result.deleted),
                                 ("Kept: not confirmed in S3", result.kept_not_uploaded),
                                 ("Damaged, kept for inspection", result.damaged),
                                 ("Removed unfinished", result.partials_removed)):  # fmt: skip
                if names:
                    print(f"{label}: {', '.join(names)}")
            summary = {
                "kept": result.kept,
                "deleted": result.deleted,
                "kept_not_uploaded": result.kept_not_uploaded,
                "damaged": result.damaged,
            }
            _record(settings, "backup_prune", started, "succeeded", summary)
        elif command == "upload":
            keys = backup.upload_backup(_s3_store(), Path(args.directory), system_clock())
            for key in keys:
                print(f"  {key}")
            print(f"Uploaded {len(keys)} files to S3 (encrypted with S3-managed keys) "
                  "and confirmed them.")  # fmt: skip
        elif command == "list":
            found = backup.list_backups(_s3_store())
            for stamp, parts in found.items():
                print(f"  {stamp}  {', '.join(parts)}")
            print(f"{len(found)} backup(s) in S3.")
        elif command == "fetch":
            directory = backup.fetch_backup(_s3_store(), args.stamp, Path(args.dir))
            print(f"Downloaded and verified {directory}.")
        elif command == "verify":
            manifest = backup.verify_backup(Path(args.directory))
            print(f"Backup {manifest['stamp']} is complete and matches its checksums.")
        else:
            target = replace(settings, db_name=args.database)
            result = backup.restore_backup(target, Path(args.directory))
            print(f"Restored into {result.database}: {result.postings:,} postings, "
                  f"{result.owner_accounts} owner account(s), {result.migrations_applied} "
                  f"migrations (backup recorded {result.migrations_expected}).")  # fmt: skip
            print("Next, with DB_NAME set to this database: jobmarket migrate; jobmarket "
                  "analytics grant; jobmarket private grant; jobmarket private check-access; "
                  "jobmarket analytics publish.")  # fmt: skip
    except (backup.BackupError, S3Unavailable, ConfigError, OSError, psycopg.Error) as exc:
        print(f"Backup error: {exc}", file=sys.stderr)
        if command in ("create", "prune"):
            step = "backup" if command == "create" else "backup_prune"
            _record(settings, step, started, "failed", error=f"{type(exc).__name__}: {exc}")
        return EXIT_CONFIG_ERROR if isinstance(exc, ConfigError) else EXIT_FAILURE
    return EXIT_OK


def _summary(settings: Settings, args) -> int:
    """Queue today's summary (once per Chicago day), deliver pending email, ping the
    heartbeat."""
    from jobmarket import summary

    started = system_clock()
    with db.connect(settings, autocommit=True) as conn:
        try:
            result, notification_id = summary.queue_daily(conn, started)
        except psycopg.Error as exc:
            _record(settings, "daily_summary", started, "failed", error=str(exc)[:500])
            raise
        if args.print:
            print(result.subject)
            print(result.body)
        if notification_id is None:
            print(f"The summary for {result.day} was already queued; nothing new.")
        else:
            print(f"Queued the summary for {result.day}: {result.subject}")
        _record(settings, "daily_summary", started, "succeeded",
                {"day": result.day, "queued": notification_id is not None,
                 "attention": result.attention})  # fmt: skip
        sent = send_pending(conn, smtp_settings(), system_clock())
    if not sent.configured:
        print("Email delivery is not configured; the summary stays in the outbox.")
    else:
        print(f"Email: sent {sent.sent}, failed {sent.failed}, pending {sent.pending_left}.")
    problem = summary.ping_heartbeat()
    if problem:
        print(problem, file=sys.stderr)
    return EXIT_FAILURE if sent.failed or problem else EXIT_OK


def _tableau(settings: Settings, args) -> int:
    """Copy the published dashboard tables to the Google Sheet behind Tableau Public."""
    from jobmarket import sheets
    from jobmarket.web.config import load_web_config

    started = system_clock()
    reader = load_web_config().settings  # the analytics login
    if args.dry_run:
        with db.connect(reader) as conn:
            conn.read_only = True
            tables = sheets.read_tables(conn)
        for table in tables:
            print(f"  {table.name:16} {len(table.rows):>7,} rows x {len(table.columns)} columns")
        print("Dry run: nothing was sent to Google.")
        return EXIT_OK
    try:
        config = sheets.load_sheets_config()
    except sheets.SheetsNotConfigured as exc:
        print(f"Not synced: {exc}.", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    try:
        with db.connect(reader) as conn:
            conn.read_only = True
            result = sheets.sync(conn, sheets.SheetsApi(config))
    except (sheets.SheetsError, psycopg.Error) as exc:
        print(f"Sheet not updated (it keeps the previous data): {exc}", file=sys.stderr)
        _record(settings, "sheets_sync", started, "failed", error=str(exc)[:2000])
        return EXIT_FAILURE
    for name, count in result.tabs.items():
        print(f"  {name:16} {count:>7,} rows")
    print(f"Updated the dashboard Sheet ({result.cells:,} cells).")
    _record(settings, "sheets_sync", started, "succeeded",
            {"tabs": result.tabs, "cells": result.cells})  # fmt: skip
    return EXIT_OK


def _sync_raw(settings: Settings, args) -> int:
    from jobmarket.connectors import SOURCE_SYSTEMS
    from jobmarket.s3store import S3Unavailable, sync_raw

    started = system_clock()
    try:
        result = sync_raw(_s3_store(), raw_dir(), SOURCE_SYSTEMS)
    except (S3Unavailable, ConfigError, OSError) as exc:
        print(f"Raw upload failed: {exc}", file=sys.stderr)
        _record(settings, "raw_sync", started, "failed", error=f"{type(exc).__name__}: {exc}")
        return EXIT_CONFIG_ERROR if isinstance(exc, ConfigError) else EXIT_FAILURE
    print(f"Uploaded {result.uploaded} raw response file(s) to S3; "
          f"{result.already_present} were already there.")  # fmt: skip
    _record(settings, "raw_sync", started, "succeeded",
            {"uploaded": result.uploaded, "already_present": result.already_present})  # fmt: skip
    return EXIT_OK


def _analytics_publish(settings: Settings, args) -> int:
    """Build in shadow schemas, validate, publish atomically, and record the outcome (which
    alerts on the first failure and sends one recovery notice after it)."""
    from jobmarket import analytics, operations

    target = getattr(args, "target", "analytics")
    builder = _analytics_login(settings) if target == "analytics" else settings
    started = system_clock()
    try:
        with db.connect(builder, autocommit=True) as conn:
            result = analytics.build_and_publish(conn, target=target)
    except analytics.PublishBusy as exc:
        print(f"Not started: {exc}.", file=sys.stderr)
        return EXIT_BUSY
    except analytics.AnalyticsUnavailable as exc:
        result = analytics.PublishResult(False, [str(exc)])
    finished = system_clock()
    with db.connect(settings, autocommit=True) as conn:
        if result.published:
            operations.record_step(conn, "report_publish", started, finished, "succeeded",
                                   {"built_at": result.built_at.isoformat()})  # fmt: skip
        else:
            operations.record_step(conn, "report_publish", started, finished, "failed",
                                   {"failures": result.failures[:20]},
                                   "; ".join(result.failures)[:2000] or "build failed")  # fmt: skip
    if not result.published:
        for failure in result.failures:
            print(f"  {failure}", file=sys.stderr)
        print("Not published: the website keeps the previous reports.", file=sys.stderr)
        return EXIT_FAILURE
    print(f"Published reports built {_when(result.built_at)}.")
    return EXIT_OK


def _analytics_login(settings: Settings) -> Settings:
    """The analytics login (ANALYTICS_DB_USER and ANALYTICS_DB_PASSWORD): it reads the
    ingestion tables and writes only its own analytics schemas."""
    from dataclasses import replace

    from jobmarket.analytics import ANALYTICS_ROLE

    login = os.environ.get("ANALYTICS_DB_USER", "").strip() or ANALYTICS_ROLE
    return replace(settings, db_user=login, db_password=os.environ.get("ANALYTICS_DB_PASSWORD", ""))


def _spark(settings: Settings, args) -> int:
    directory = Path(args.dir)
    if args.spark_command == "export":
        return _spark_export(settings, directory)
    try:
        from jobmarket import spark_models
    except ImportError:
        print('Spark commands need PySpark: pip install -e ".[spark]"', file=sys.stderr)
        return EXIT_CONFIG_ERROR
    if shutil.which("java") is None and not os.environ.get("JAVA_HOME"):
        print("Spark needs Java 17 or 21 on PATH or in JAVA_HOME.", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    if args.spark_command == "benchmark":
        return _spark_benchmark(settings, args)
    if not (directory / "export.json").exists():
        print(f"No export in {directory}: run `jobmarket spark export` first.", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    spark = spark_models.local_session()
    rebuilt = spark_models.build(spark_models.read_inputs(spark, str(directory)))
    expected = spark_models.read_dbt(spark, str(directory))
    matched = True
    for model in spark_models.MODELS:
        found = spark_models.differences(rebuilt[model], expected[model])
        verdict = "different" if found else "match"
        print(
            f"  {model:18} dbt {expected[model].count():>9,} rows, "
            f"spark {rebuilt[model].count():>9,} rows: {verdict}"
        )
        for line in found:
            print(f"    {line}")
        matched = matched and not found
    if not matched:
        print("Spark and dbt differ.", file=sys.stderr)
        return EXIT_FAILURE
    print("All three models match dbt row for row.")
    return EXIT_OK


def _spark_benchmark(settings: Settings, args) -> int:
    from jobmarket.analytics import AnalyticsUnavailable

    try:
        from jobmarket import spark_benchmark
    except ImportError:
        print('The benchmark needs dbt: pip install -e ".[analytics,spark]"', file=sys.stderr)
        return EXIT_CONFIG_ERROR
    scales = [int(value) for value in args.scales.split(",")]
    try:
        result = spark_benchmark.benchmark(
            _analytics_login(settings), Path(args.dir), args.runs, scales
        )
    except (AnalyticsUnavailable, spark_benchmark.spark_export.ExportError) as exc:
        print(f"Not measured: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(spark_benchmark.summary(result))
    print(f"Results written to {output}.")
    different = [s for s, r in result["scales"].items() if any(r["parity"].values())]
    if different:
        print(f"Spark and PostgreSQL differ at scale {', '.join(different)}.", file=sys.stderr)
        return EXIT_FAILURE
    return EXIT_OK


def _spark_export(settings: Settings, directory: Path) -> int:
    from jobmarket.analytics import AnalyticsUnavailable

    try:
        from jobmarket import spark_export
    except ImportError:
        print('The export needs PyArrow: pip install -e ".[analytics,spark]"', file=sys.stderr)
        return EXIT_CONFIG_ERROR
    try:
        reference = spark_export.reference_sql()
    except (AnalyticsUnavailable, spark_export.ExportError) as exc:
        print(f"Not exported: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    with db.connect(_analytics_login(settings)) as conn:
        summary = spark_export.export(conn, directory, reference)
    for relative, info in summary["files"].items():
        print(
            f"  {relative:38} {info['rows']:>10,} rows {info['bytes'] / 1e6:>9.2f} MB "
            f"{info['seconds']:>8.2f} s"
        )
    snapshot = datetime.fromisoformat(summary["snapshot"])
    print(f"Exported the database as of {_when(snapshot)} to {directory}.")
    return EXIT_OK


def _refingerprint(settings: Settings, args) -> int:
    with db.connect(settings, autocommit=True) as conn:
        result = refingerprint_versions(conn, system_clock())
    print(
        f"Evaluated {result.versions} version(s) under normalization rules {result.rules} "
        f"({result.computed} newly computed). No versions were created or changed."
    )
    if result.omitted:
        print(
            f"{result.omitted} version(s) were skipped: their long description was not stored, "
            "so they are compared by fingerprint only."
        )
    if result.rule_effects:
        print(
            f"{len(result.rule_effects)} version(s) have a different comparison form under "
            "these rules than under the rules they were recorded with. The stored content is "
            "the same, so this is an effect of the rules, not an employer edit:"
        )
        for version_id, rules in result.rule_effects:
            print(f"  version {version_id} (recorded under {rules})")
    return EXIT_OK


def _check_history(settings: Settings, args) -> int:
    with db.connect(settings) as conn:
        problems = conn.execute(
            "SELECT posting_id, problem FROM posting_history_problems ORDER BY posting_id"
        ).fetchall()
        totals = conn.execute(
            "SELECT (SELECT count(*) FROM source_postings), "
            "(SELECT count(*) FROM posting_versions), "
            "(SELECT count(*) FROM posting_observations)"
        ).fetchone()
    print(
        f"Checked {totals[0]} postings, {totals[1]} versions, {totals[2]} observations: "
        f"{len(problems)} problem(s)."
    )
    for posting_id, problem in problems:
        print(f"  posting {posting_id}: {problem}")
    return EXIT_FAILURE if problems else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobmarket", description="Job Market Intelligence")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check-db", help="connect to PostgreSQL and run SELECT 1")
    migrate = commands.add_parser("migrate", help="apply pending database migrations")
    migrate.add_argument(
        "--status", action="store_true", help="list migrations and whether they are applied"
    )

    sources = commands.add_parser("sources", help="board registry")
    sources_commands = sources.add_subparsers(dest="sources_command", required=True)
    sources_commands.add_parser(
        "sync", help="load the board registries (config/*_boards.toml) into the database"
    )
    mapping = sources_commands.add_parser(
        "check-mapping",
        help="look for each board on its employer's official careers page (records evidence)",
    )
    mapping.add_argument("--all", action="store_true", help="include disabled boards")
    mapping.add_argument("--source", choices=["greenhouse", "lever"])
    mapping.add_argument("--board", action="append", metavar="TOKEN")
    listing = sources_commands.add_parser("list", help="show registry boards")
    listing.add_argument(
        "--all", action="store_true", help="include disabled candidates, leads, and holds"
    )

    collect = commands.add_parser("collect", help="scan enabled Greenhouse and Lever boards now")
    collect.add_argument(
        "--board",
        action="append",
        metavar="TOKEN",
        help="scan only this enabled board (repeatable; SOURCE:TOKEN picks one source)",
    )
    collect.add_argument(
        "--scheduled",
        action="store_true",
        help="record the run as scheduled (for use by a scheduler)",
    )
    commands.add_parser("retry-due", help="run the retries that are due now")

    status = commands.add_parser("status", help="per-board collection status")
    status.add_argument("--all", action="store_true", help="include disabled boards")

    jobs = commands.add_parser("jobs", help="list active retained jobs, newest discoveries first")
    jobs.add_argument(
        "--all-states", action="store_true", help="include assumed-inactive and closed jobs"
    )
    jobs.add_argument("--board", metavar="TOKEN")
    jobs.add_argument(
        "--early-career",
        action="store_true",
        help="the screening rules' early-career flag (implies --all-levels)",
    )
    jobs.add_argument("--internships", action="store_true", help="internships instead")
    jobs.add_argument("--all-levels", action="store_true", help="every level, not only freshers")
    jobs.add_argument("--review", action="store_true", help="only jobs flagged for review")
    jobs.add_argument("--limit", type=int, default=25)

    notifications = commands.add_parser("notifications", help="alert and recovery outbox")
    notifications.add_argument(
        "--send", action="store_true", help="deliver pending notifications if SMTP is configured"
    )
    notifications.add_argument(
        "--send-test", action="store_true", help="queue one test email, then send pending email"
    )
    notifications.add_argument("--full", action="store_true", help="show message bodies")
    notifications.add_argument("--limit", type=int, default=20)

    cleanup = commands.add_parser("cleanup-raw", help="delete raw responses older than 4 days")
    cleanup.add_argument("--dry-run", action="store_true")
    cleanup.add_argument(
        "--require-s3",
        action="store_true",
        help="delete only files confirmed in S3, and fail if S3 is not configured",
    )

    replay = commands.add_parser(
        "replay", help="reprocess saved raw responses under their original collection time"
    )
    which = replay.add_mutually_exclusive_group(required=True)
    which.add_argument("--scan", type=int, action="append", metavar="ID", help="repeatable")
    which.add_argument(
        "--available", action="store_true", help="every scan whose raw response is still stored"
    )

    ext = commands.add_parser("extract", help="AI requirement extraction queue and results")
    ext_cmds = ext.add_subparsers(dest="extract_command", required=True)
    ext_cmds.add_parser("status", help="queue, today's usage, pause, and results")
    ext_run = ext_cmds.add_parser("run", help="send queued listings to the model, within limits")
    ext_run.add_argument("--limit", type=int, metavar="N", help="handle at most N jobs")
    ext_run.add_argument(
        "--skip-if-worker",
        action="store_true",
        help="do nothing (exit 3) while the extraction worker is running (the hourly batch)",
    )
    ext_cmds.add_parser(
        "worker", help="extract continuously until stopped (the extraction worker service)"
    )
    ext_show = ext_cmds.add_parser("show", help="the current extraction of one listing")
    ext_show.add_argument("posting", type=int)
    ext_show.add_argument("--evidence", action="store_true", help="list every quote")
    ext_re = ext_cmds.add_parser(
        "reprocess", help="queue current content under the active configuration (explicit)"
    )
    ext_re.add_argument("--reason", required=True)
    ext_re.add_argument("--limit", type=int, metavar="N", help="queue at most N listings")
    ext_re.add_argument("--retained-only", action="store_true")
    ext_cmds.add_parser(
        "renormalize", help="apply the current skill/location clean-up to stored answers"
    )
    ext_dis = ext_cmds.add_parser(
        "disagreements", help="listings where the AI and the screening rules disagree"
    )
    ext_dis.add_argument("--limit", type=int, default=30)

    ev = commands.add_parser("eval", help="evaluate extraction against labelled cases")
    ev_cmds = ev.add_subparsers(dest="eval_command", required=True)
    ev_prep = ev_cmds.add_parser("prepare", help="write case files for a selection (JSON)")
    ev_prep.add_argument("--selection", required=True, type=Path)
    for name, text in (("run", "extract the cases of one split"),
                       ("report", "compare stored results with the labels")):  # fmt: skip
        cmd = ev_cmds.add_parser(name, help=text)
        cmd.add_argument("--split", required=True, choices=["practice", "holdout"])
        cmd.add_argument("--final", action="store_true", help="required for the holdout")
        if name == "report":
            cmd.add_argument("--cases", action="store_true", help="list every mismatch")

    an = commands.add_parser("analytics", help="dbt analytics models (analytics/)")
    an_cmds = an.add_subparsers(dest="analytics_command", required=True)
    an_build = an_cmds.add_parser("build", help="rebuild and test every analytics model")
    an_build.add_argument("--select", metavar="SELECTOR", help="dbt selector, e.g. reporting")
    an_publish = an_cmds.add_parser(
        "publish", help="build and test in shadow schemas, then replace the live ones at once"
    )
    an_cmds.add_parser("test", help="run the analytics data tests only")
    an_cmds.add_parser("docs", help="generate dbt documentation and the dependency graph")
    an_cmds.add_parser("grant", help="give the analytics login read-only access (owner)")
    an_cmds.add_parser("check-access", help="show what the analytics login may do")
    for cmd in (an_build, an_publish):
        cmd.add_argument(
            "--target",
            default="analytics",
            choices=["analytics", "owner"],
            help="database login (owner is for tests and CI)",
        )

    own = commands.add_parser("owner", help="the single owner account of the private workspace")
    own_cmds = own.add_subparsers(dest="owner_command", required=True)
    create = own_cmds.add_parser("create", help="create the owner account (asks for a password)")
    create.add_argument("--username", required=True)
    own_cmds.add_parser("reset-password", help="recovery: set a new password, end all sessions")
    own_cmds.add_parser("status", help="whether the account exists, sessions, sign-in pause")
    own_cmds.add_parser("sign-out-all", help="end every session")

    priv = commands.add_parser("private", help="the private login's database access")
    priv_cmds = priv.add_subparsers(dest="private_command", required=True)
    priv_cmds.add_parser("grant", help="let jobmarket_private use the private schema only")
    priv_cmds.add_parser("check-access", help="verify which logins can reach private data")

    web = commands.add_parser("web", help="run the job-search website on this computer")
    web.add_argument(
        "--host", default="127.0.0.1", help="address to listen on (default: local only)"
    )
    web.add_argument("--port", type=int, default=8000)

    listing = commands.add_parser("listing", help="one listing as a detail page would show it")
    listing.add_argument("posting", type=int)
    listing.add_argument("--html", action="store_true", help="print the display-safe HTML")
    history = commands.add_parser("history", help="content history of one posting")
    history.add_argument("posting", nargs="?", type=int, help="posting id")
    history.add_argument("--board", metavar="TOKEN")
    history.add_argument("--job", metavar="SOURCE_ID", help="the source's job id")

    bk = commands.add_parser("backup", help="database backups (local, S3) and restores")
    bk_cmds = bk.add_subparsers(dest="backup_command", required=True)
    bk_create = bk_cmds.add_parser("create", help="dump the public and private schemas")
    bk_create.add_argument("--dir", default="data/backups", help="parent directory")
    bk_create.add_argument("--upload", action="store_true", help="then upload it to S3")
    bk_upload = bk_cmds.add_parser("upload", help="upload an existing backup directory to S3")
    bk_upload.add_argument("directory")
    bk_cmds.add_parser("list", help="backups stored in S3")
    bk_fetch = bk_cmds.add_parser("fetch", help="download one backup from S3 and verify it")
    bk_fetch.add_argument("stamp", help="the backup's name, like 20261101T060000Z")
    bk_fetch.add_argument("--dir", default="data/backups", help="parent directory")
    bk_prune = bk_cmds.add_parser("prune", help="bound local backups (keeps the newest)")
    bk_prune.add_argument("--keep", type=int, required=True, metavar="N",
                          help="newest verified backups to keep")  # fmt: skip
    bk_prune.add_argument("--dir", default="data/backups", help="parent directory")
    bk_verify = bk_cmds.add_parser("verify", help="check a backup directory's checksums")
    bk_verify.add_argument("directory")
    bk_restore = bk_cmds.add_parser("restore", help="restore into a new, empty database")
    bk_restore.add_argument("directory")
    bk_restore.add_argument("--database", required=True, help="the empty target database")

    commands.add_parser("sync-raw", help="upload raw responses not yet in S3")

    tab = commands.add_parser("tableau", help="data for the Tableau Public dashboards")
    tab_cmds = tab.add_subparsers(dest="tableau_command", required=True)
    tab_sync = tab_cmds.add_parser(
        "sync", help="copy the published dashboard tables to the Google Sheet (all at once)"
    )
    tab_sync.add_argument(
        "--dry-run", action="store_true", help="show what would be sent, without contacting Google"
    )

    sp = commands.add_parser("spark", help="three reporting models rebuilt in PySpark")
    sp_cmds = sp.add_subparsers(dest="spark_command", required=True)
    sp_export = sp_cmds.add_parser(
        "export", help="write the models' inputs and dbt's results to Parquet (one snapshot)"
    )
    sp_parity = sp_cmds.add_parser("parity", help="rebuild the models in Spark, compare with dbt")
    sp_bench = sp_cmds.add_parser(
        "benchmark", help="time PostgreSQL, dbt, and Spark on the same inputs (several minutes)"
    )
    sp_bench.add_argument("--runs", type=int, default=5, help="runs per measurement")
    sp_bench.add_argument(
        "--scales", default="1,10,100", help="copies of the data to measure, comma separated"
    )
    sp_bench.add_argument("--output", default="docs/spark/results/benchmark.json")
    for cmd in (sp_export, sp_parity, sp_bench):
        cmd.add_argument("--dir", default="data/spark", help="export directory")

    summ = commands.add_parser("summary", help="the daily summary email")
    summ_cmds = summ.add_subparsers(dest="summary_command", required=True)
    summ_send = summ_cmds.add_parser(
        "send", help="queue today's summary (once a day), send pending email, ping heartbeat"
    )
    summ_send.add_argument("--print", action="store_true", help="also print the summary")

    commands.add_parser(
        "refingerprint",
        help="evaluate stored versions under the current normalization rules",
    )
    commands.add_parser("check-history", help="verify history and current-state consistency")

    dup = commands.add_parser("duplicates", help="duplicate groups and possible duplicates")
    dup_cmds = dup.add_subparsers(dest="duplicates_command", required=True)
    dup_list = dup_cmds.add_parser("list", help="active groups and open possible duplicates")
    dup_list.add_argument("--all", action="store_true", help="include reviewed and dissolved")
    for name, what in (
        ("confirm", "confirm a possible duplicate and group the two records"),
        ("dismiss", "dismiss a possible duplicate (the rule will not raise it again)"),
        ("dissolve", "undo a duplicate group (the rule will not recreate it)"),
    ):
        cmd = dup_cmds.add_parser(name, help=what)
        cmd.add_argument("id", type=int, help="group id" if name == "dissolve" else "flag id")
        cmd.add_argument("--reason", required=True)

    avail = commands.add_parser("availability", help="posting availability and board holds")
    avail_cmds = avail.add_subparsers(dest="availability_command", required=True)
    show = avail_cmds.add_parser("show", help="state and transition history of one posting")
    show.add_argument("posting", type=int)
    close = avail_cmds.add_parser("close", help="mark a posting closed on official evidence")
    close.add_argument("posting", type=int)
    close.add_argument("--reason", required=True)
    close.add_argument("--evidence-url", required=True)
    close.add_argument("--evidence-text", required=True, help="what the official source says")
    holds = avail_cmds.add_parser("holds", help="board holds against mass disappearances")
    holds.add_argument("--all", action="store_true", help="include released holds")
    release = avail_cmds.add_parser("release", help="release a board hold after review")
    release.add_argument("hold", type=int)
    release.add_argument(
        "--reason",
        required=True,
        choices=["collection-issue-resolved", "source-removals-verified"],
    )
    release.add_argument("--evidence", required=True, help="what was checked and found")

    pages = commands.add_parser(
        "check-pages",
        help="GET a sample of retained postings' job and application pages (never submits)",
    )
    pages.add_argument("--per-board", type=int, default=2, metavar="N")
    pages.add_argument("--source", choices=["greenhouse", "lever"])
    pages.add_argument("--board", action="append", metavar="TOKEN")
    return parser


HANDLERS = {
    "check-db": _check_db,
    "migrate": _migrate,
    "sources": _sources,
    "collect": _collect,
    "retry-due": _collect,
    "status": _status,
    "jobs": _jobs,
    "notifications": _notifications,
    "cleanup-raw": _cleanup_raw,
    "replay": _replay,
    "history": _history,
    "refingerprint": _refingerprint,
    "check-history": _check_history,
    "check-pages": _check_pages,
    "availability": _availability,
    "duplicates": _duplicates,
    "listing": _listing,
    "extract": _extract,
    "eval": _eval,
    "analytics": _analytics,
    "web": _web,
    "owner": _owner,
    "private": _private,
    "backup": _backup,
    "sync-raw": _sync_raw,
    "summary": _summary,
    "tableau": _tableau,
    "spark": _spark,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    try:
        return HANDLERS[args.command](settings, args)
    except MigrationError as exc:
        print(f"Migration error: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except RegistryError as exc:
        print(f"Registry error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except psycopg.Error as exc:
        print(f"Database error for {_describe(settings)}: {exc}".rstrip(), file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
