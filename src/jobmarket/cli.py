"""Command-line entry point: ``jobmarket <command>``."""

import argparse
import sys
from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg

from jobmarket import db
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


def _jobs(settings: Settings, args) -> int:
    where, params = ["true"], []
    if args.board:
        where.append("token = %s")
        params.append(args.board)
    if args.early_career:
        where.append("early_career")
    if args.review:
        where.append("needs_review")
    view = "retained_jobs" if args.all_states else "active_jobs"
    with db.connect(settings) as conn:
        rows = conn.execute(
            "SELECT employer, title, location_text, texas_eligibility, seniority, early_career, "
            "families, sponsorship_status, needs_review, first_discovered_at, absolute_url, "
            "availability_state, qualifying_absences, posting_id, identity_basis, "
            "duplicate_group_id, open_duplicate_flags "
            f"FROM {view} WHERE {' AND '.join(where)} "
            "ORDER BY first_discovered_at DESC, employer, title LIMIT %s",
            (*params, args.limit),
        ).fetchall()
        total = conn.execute(f"SELECT count(*) FROM {view}").fetchone()[0]
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
    print(f"\nShowing {len(rows)} of {total} {shown} job(s).")
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
    with db.connect(settings, autocommit=True) as conn:
        result = cleanup_raw(conn, raw_dir(), system_clock(), RAW_RETENTION, args.dry_run)
    verb = "Would delete" if result.dry_run else "Deleted"
    print(
        f"{verb} {result.files_deleted} raw response file(s) older than "
        f"{RAW_RETENTION.days} days ({result.files_already_missing} already missing; "
        f"{result.purged_rows} row(s) {'to mark' if result.dry_run else 'marked'} purged) "
        f"and {result.orphan_files_deleted} orphan file(s). Job records are not touched."
    )
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
    jobs.add_argument("--early-career", action="store_true")
    jobs.add_argument("--review", action="store_true", help="only jobs flagged for review")
    jobs.add_argument("--limit", type=int, default=25)

    notifications = commands.add_parser("notifications", help="alert and recovery outbox")
    notifications.add_argument(
        "--send", action="store_true", help="deliver pending notifications if SMTP is configured"
    )
    notifications.add_argument("--full", action="store_true", help="show message bodies")
    notifications.add_argument("--limit", type=int, default=20)

    cleanup = commands.add_parser("cleanup-raw", help="delete raw responses older than 4 days")
    cleanup.add_argument("--dry-run", action="store_true")

    replay = commands.add_parser(
        "replay", help="reprocess saved raw responses under their original collection time"
    )
    which = replay.add_mutually_exclusive_group(required=True)
    which.add_argument("--scan", type=int, action="append", metavar="ID", help="repeatable")
    which.add_argument(
        "--available", action="store_true", help="every scan whose raw response is still stored"
    )

    history = commands.add_parser("history", help="content history of one posting")
    history.add_argument("posting", nargs="?", type=int, help="posting id")
    history.add_argument("--board", metavar="TOKEN")
    history.add_argument("--job", metavar="SOURCE_ID", help="the source's job id")

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
    except psycopg.Error as exc:
        print(f"Database error for {_describe(settings)}: {exc}".rstrip(), file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
