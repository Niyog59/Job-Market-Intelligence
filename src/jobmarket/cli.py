"""Command-line entry point: ``jobmarket <command>``."""

import argparse
import sys
from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg

from jobmarket import db
from jobmarket.clock import system_clock
from jobmarket.collector import CollectionBusy, Collector, make_client, request_interval, summarize
from jobmarket.config import ConfigError, Settings, load_settings
from jobmarket.incidents import next_retry_time
from jobmarket.migrate import MigrationError, apply_migrations, migration_status
from jobmarket.notify import send_pending, smtp_settings
from jobmarket.raw_store import RAW_RETENTION, cleanup_raw, raw_dir
from jobmarket.sources import RegistryError, load_registry, registry_path, sync_registry

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


def _sources(settings: Settings, args) -> int:
    registry = load_registry()
    if args.sources_command == "sync":
        with db.connect(settings, autocommit=True) as conn:
            result = sync_registry(conn, registry, system_clock())
        print(
            f"Synced {registry_path()}: {result.boards_upserted} boards "
            f"({result.boards_enabled} enabled) for {result.employers_upserted} employers."
        )
        if result.boards_not_in_registry:
            print(
                "Disabled boards no longer in the registry: "
                + ", ".join(result.boards_not_in_registry)
            )
        return EXIT_OK

    boards = registry.boards if args.all else registry.enabled
    print(f"{'token':30} {'employer':34} {'region':6} {'status':9} metros")
    for b in boards:
        state = "enabled" if b.enabled else b.verification
        print(f"{b.token:30} {b.employer[:34]:34} {b.region:6} {state:9} {','.join(b.metros)}")
    if args.all:
        print(
            f"\n{len(registry.boards)} boards: {len(registry.enabled)} enabled, "
            f"{len(registry.boards) - len(registry.enabled)} disabled candidates."
        )
        print("\nUnverified leads and holds (not collected):")
        for lead in registry.leads:
            tokens = ", ".join(lead.tokens) or "token not found yet"
            print(
                f"  [{lead.status}] {lead.employer}: {tokens}"
                + (f" ({lead.notes})" if lead.notes else "")
            )
    return EXIT_OK


def _collect(settings: Settings, args) -> int:
    with db.connect(settings, autocommit=True) as conn, make_client() as client:
        collector = Collector(conn, client, raw_dir(), interval=request_interval())
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
            "SELECT employer, token, enabled, last_scan_status, last_scan_finished_at, "
            "last_complete_at, last_usable_at, retained_postings, incident_status, "
            "retries_used, next_retry_at, alerted, last_error FROM board_status "
            + ("" if args.all else "WHERE enabled ")
            + "ORDER BY employer, token"
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
        f"{'employer':32} {'token':26} {'last scan':9} {'finished':21} {'last complete':21} "
        f"{'kept':>5}  incident"
    )
    for (
        employer,
        token,
        enabled,
        last,
        finished,
        complete,
        _usable,
        retained,
        inc_status,
        retries,
        next_retry,
        alerted,
        error,
    ) in rows:
        incident = "-"
        if inc_status == "retrying":
            incident = f"retrying ({retries}/3 used, next {_when(next_retry)})"
        elif inc_status == "exhausted":
            incident = f"retries exhausted ({'alert queued' if alerted else 'no alert yet'})"
        name = employer[:32] + ("" if enabled else " (disabled)")
        print(
            f"{name[:32]:32} {token[:26]:26} {last or 'never':9} {_when(finished):21} "
            f"{_when(complete):21} {retained:>5}  {incident}"
        )
        if last in ("failed", "partial") and error:
            print(f"    {last}: {error[:150]}")
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
    with db.connect(settings) as conn:
        rows = conn.execute(
            "SELECT employer, title, location_text, texas_eligibility, seniority, early_career, "
            "families, sponsorship_status, needs_review, first_discovered_at, absolute_url "
            f"FROM retained_jobs WHERE {' AND '.join(where)} "
            "ORDER BY first_discovered_at DESC, employer, title LIMIT %s",
            (*params, args.limit),
        ).fetchall()
        total = conn.execute("SELECT count(*) FROM retained_jobs").fetchone()[0]
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
    ) in rows:
        flags = [eligibility, seniority]
        if early:
            flags.append("early-career")
        if review:
            flags.append("NEEDS REVIEW")
        if sponsorship != "not_stated":
            flags.append(f"sponsorship: {sponsorship}")
        print(f"{employer}: {title}")
        print(f"    {location or 'no location'} | {', '.join(flags)} | {', '.join(families)}")
        print(f"    discovered {_when(discovered)} | {url}")
    print(f"\nShowing {len(rows)} of {total} retained job(s).")
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
    sources_commands.add_parser("sync", help="load config/greenhouse_boards.toml into the database")
    listing = sources_commands.add_parser("list", help="show registry boards")
    listing.add_argument(
        "--all", action="store_true", help="include disabled candidates, leads, and holds"
    )

    collect = commands.add_parser("collect", help="scan enabled Greenhouse boards now")
    collect.add_argument(
        "--board",
        action="append",
        metavar="TOKEN",
        help="scan only this enabled board (repeatable)",
    )
    collect.add_argument(
        "--scheduled",
        action="store_true",
        help="record the run as scheduled (for use by a scheduler)",
    )
    commands.add_parser("retry-due", help="run the retries that are due now")

    status = commands.add_parser("status", help="per-board collection status")
    status.add_argument("--all", action="store_true", help="include disabled boards")

    jobs = commands.add_parser("jobs", help="list retained jobs, newest discoveries first")
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
