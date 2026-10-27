"""Collection runs across job boards of every supported source (Greenhouse, Lever).

One run scans a set of boards. Fetching happens in worker threads, one lane per source
system by default, so the two providers are read at the same time while requests to each
provider stay paced (Pacer). Each fetched board is then processed in the main thread, in a
fixed order: start a board_scans row, store every raw page, validate through the board's
connector, store usable records in one transaction, and update the board's retry
incident. All database work stays on one connection in one thread. A problem with one
board is recorded on that board and never stops the others. Failed and partial scans
never mark anything closed or remove records.

Only one collection can run at a time (a PostgreSQL advisory lock). Because the lock
guarantees no other collector is active, any run or scan still marked "running" at start
was abandoned by a stopped process; it is marked failed so its retry state continues.
"""

import os
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import psycopg
from psycopg.types.json import Jsonb

from jobmarket import incidents
from jobmarket.clock import Clock, system_clock
from jobmarket.connectors import (
    USER_AGENT,
    BoardFetch,
    Pacer,
    Page,
    ParsedResponse,
    get_connector,
    saved_pages,
)
from jobmarket.raw_store import store_body
from jobmarket.store import StoreCounts, flag_possible_duplicates, store_scan_results

COLLECTION_LOCK_KEY = 4_815_162_343
REQUEST_INTERVAL_ENV = "JOBMARKET_REQUEST_INTERVAL"
# Minimum seconds between two requests to the same provider, across all its workers.
DEFAULT_REQUEST_INTERVAL = 1.0
CONCURRENCY_ENV = "JOBMARKET_CONCURRENCY"
# Boards of one provider fetched at the same time. Pacing still spaces their requests.
DEFAULT_CONCURRENCY = 1
MAX_CONCURRENCY = 4


SUPPORTED_SOURCES = ("greenhouse", "lever")


class CollectionBusy(Exception):
    """Another collection or replay is already running."""


@contextmanager
def collection_lock(conn: psycopg.Connection) -> Iterator[None]:
    """Hold the application-wide processing lock, or raise CollectionBusy at once.

    Collections and replays both take it, so they never interleave. Correctness does not
    depend on it (constraints and row locks keep concurrent writers consistent); it keeps
    runs and their summaries from overlapping.
    """
    got = conn.execute("SELECT pg_try_advisory_lock(%s)", (COLLECTION_LOCK_KEY,)).fetchone()[0]
    if not got:
        raise CollectionBusy("another collection or replay is already running")
    try:
        yield
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (COLLECTION_LOCK_KEY,))


def make_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=False,
    )


def request_interval() -> float:
    return float(os.environ.get(REQUEST_INTERVAL_ENV, DEFAULT_REQUEST_INTERVAL))


def concurrency() -> int:
    """Workers per source system, from JOBMARKET_CONCURRENCY, kept between 1 and 4."""
    value = int(os.environ.get(CONCURRENCY_ENV, DEFAULT_CONCURRENCY))
    return max(1, min(MAX_CONCURRENCY, value))


@dataclass(frozen=True)
class BoardTarget:
    board_id: int
    token: str
    employer: str
    source_system: str = "greenhouse"
    region: str = "us"


@dataclass
class BoardOutcome:
    token: str
    employer: str
    board_scan_id: int
    status: str
    attempt_kind: str
    error: str | None
    source_system: str = "greenhouse"
    pages: int | None = None
    reported: int | None = None
    received: int | None = None
    invalid: int | None = None
    counts: StoreCounts = field(default_factory=StoreCounts)
    incident_event: str = "none"
    incident_id: int | None = None


@dataclass
class RunReport:
    run_id: int | None
    trigger: str
    status: str | None
    outcomes: list[BoardOutcome] = field(default_factory=list)
    notifications: list[int] = field(default_factory=list)
    abandoned_scans_recovered: int = 0
    duplicate_flags_added: int = 0


class Collector:
    def __init__(
        self,
        conn: psycopg.Connection,
        client: httpx.Client,
        raw_root: Path,
        clock: Clock = system_clock,
        sleep: Callable[[float], None] = time.sleep,
        interval: float = DEFAULT_REQUEST_INTERVAL,
        workers_per_source: int = DEFAULT_CONCURRENCY,
    ) -> None:
        if not conn.autocommit:
            raise ValueError("the collector needs an autocommit connection")
        self.conn = conn
        self.client = client
        self.raw_root = raw_root
        self.clock = clock
        self.sleep = sleep
        self.interval = interval
        self.workers_per_source = max(1, min(MAX_CONCURRENCY, workers_per_source))

    # --- public ----------------------------------------------------------------------

    def collect(self, trigger: str = "manual", tokens: list[str] | None = None) -> RunReport:
        """Scan every enabled board, or only the given enabled tokens."""
        with collection_lock(self.conn):
            events, abandoned = self._recover_abandoned()
            return self._run(trigger, self._targets(tokens), events, abandoned)

    def retry_due(self) -> RunReport:
        """Scan only boards whose next retry is due now."""
        with collection_lock(self.conn):
            events, abandoned = self._recover_abandoned()
            due = set(incidents.due_retry_board_ids(self.conn, self.clock()))
            targets = [t for t in self._targets(None) if t.board_id in due]
            return self._run("retry", targets, events, abandoned)

    # --- internals -------------------------------------------------------------------

    def _targets(self, tokens: list[str] | None) -> list[BoardTarget]:
        rows = self.conn.execute(
            "SELECT b.board_id, b.board_key, e.name, b.is_active, b.source_system, "
            "b.hosting_region FROM source_boards b JOIN employers e USING (employer_id) "
            "WHERE b.source_system = ANY(%s) ORDER BY e.name, b.source_system, b.board_key",
            (list(SUPPORTED_SOURCES),),
        ).fetchall()
        if tokens:
            # A token names a board of any source; "source:token" picks one source.
            by_token: dict[str, tuple] = {}
            for r in rows:
                by_token.setdefault(r[1], r)
                by_token[f"{r[4]}:{r[1]}"] = r
            unknown = [t for t in tokens if t not in by_token]
            inactive = [t for t in tokens if t in by_token and not by_token[t][3]]
            if unknown or inactive:
                raise ValueError(
                    "cannot collect: "
                    + "; ".join(
                        filter(
                            None,
                            [
                                f"unknown board(s) {unknown}" if unknown else "",
                                f"disabled board(s) {inactive}" if inactive else "",
                            ],
                        )
                    )
                )
            rows = [by_token[t] for t in tokens]
        return [BoardTarget(r[0], r[1], r[2], r[4], r[5]) for r in rows if r[3]]

    def _recover_abandoned(self) -> tuple[list[incidents.IncidentEvent], int]:
        now = self.clock()
        events = []
        stuck = self.conn.execute(
            "SELECT board_scan_id, board_id, attempt_kind FROM board_scans "
            "WHERE status = 'running' ORDER BY board_scan_id"
        ).fetchall()
        for scan_id, board_id, kind in stuck:
            with self.conn.transaction():
                self.conn.execute(
                    "UPDATE board_scans SET status = 'failed', finished_at = %s, "
                    "error_message = 'abandoned: the collector stopped before finishing "
                    "this scan' WHERE board_scan_id = %s",
                    (now, scan_id),
                )
                events.append(
                    incidents.record_outcome(
                        self.conn,
                        board_id,
                        scan_id,
                        "failed",
                        "abandoned scan (collector stopped)",
                        kind,
                        now,
                    )
                )
        self.conn.execute(
            "UPDATE collection_runs SET status = 'failed', finished_at = %s "
            "WHERE status = 'running'",
            (now,),
        )
        return events, len(stuck)

    def _run(
        self,
        trigger: str,
        targets: list[BoardTarget],
        events: list[incidents.IncidentEvent],
        abandoned: int,
    ) -> RunReport:
        report = RunReport(None, trigger, None, abandoned_scans_recovered=abandoned)
        if not targets:
            if events:
                with self.conn.transaction():
                    report.notifications = incidents.queue_notifications(
                        self.conn, None, "failed", events, self.clock()
                    )
            return report

        started = self.clock()
        run_id = self.conn.execute(
            "INSERT INTO collection_runs (trigger, started_at, status) "
            "VALUES (%s, %s, 'running') RETURNING run_id",
            (trigger, started),
        ).fetchone()[0]
        report.run_id = run_id

        for target, fetch in self._fetch_all(targets):
            outcome = self._collect_board(run_id, target, fetch)
            report.outcomes.append(outcome)
            events.append(incidents.IncidentEvent(outcome.incident_event, outcome.incident_id))

        statuses = {o.status for o in report.outcomes}
        if statuses == {"complete"}:
            status = "complete"
        elif statuses == {"failed"}:
            status = "failed"
        else:
            status = "partial"
        report.status = status

        finished = self.clock()
        with self.conn.transaction():
            report.duplicate_flags_added = flag_possible_duplicates(self.conn, finished)
            report.notifications = incidents.queue_notifications(
                self.conn, run_id, status, events, finished
            )
            self.conn.execute(
                "UPDATE collection_runs SET status = %s, finished_at = %s WHERE run_id = %s",
                (status, finished, run_id),
            )
        return report

    def _fetch_all(self, targets: list[BoardTarget]) -> Iterator[tuple[BoardTarget, BoardFetch]]:
        """Fetch every target, in parallel lanes per source system, and yield the results in
        target order. Nothing here touches the database."""
        with ExitStack() as stack:
            pools: dict[str, ThreadPoolExecutor] = {}
            pacers: dict[str, Pacer] = {}
            futures: list[Future] = []
            for target in targets:
                source = target.source_system
                if source not in pools:
                    pools[source] = stack.enter_context(
                        ThreadPoolExecutor(self.workers_per_source, f"fetch-{source}")
                    )
                    pacers[source] = Pacer(self.interval, self.sleep)
                futures.append(pools[source].submit(self._fetch, target, pacers[source]))
            for target, future in zip(targets, futures, strict=True):
                yield target, future.result()

    def _fetch(self, target: BoardTarget, pacer: Pacer) -> BoardFetch:
        connector = get_connector(target.source_system)
        try:
            return connector.fetch(
                self.client, target.token, target.region, self.clock, pacer, self.sleep
            )
        except Exception as exc:  # noqa: BLE001 - a connector bug must not stop the run
            error = f"internal error while fetching: {type(exc).__name__}: {exc}"[:1000]
            url = connector.board_url(target.token, target.region)
            return BoardFetch((Page(1, url, self.clock(), None, None, {}, error),))

    def _collect_board(self, run_id: int, target: BoardTarget, fetch: BoardFetch) -> BoardOutcome:
        now = self.clock()
        kind = incidents.attempt_kind(incidents.open_incident(self.conn, target.board_id), now)
        scan_id = self.conn.execute(
            "INSERT INTO board_scans (run_id, board_id, started_at, status, attempt_kind, "
            "pages_fetched) VALUES (%s, %s, %s, 'running', %s, %s) RETURNING board_scan_id",
            (run_id, target.board_id, fetch.collected_at, kind, len(fetch.pages)),
        ).fetchone()[0]
        outcome = BoardOutcome(
            target.token, target.employer, scan_id, "failed", kind, None, target.source_system,
            len(fetch.pages),
        )  # fmt: skip
        connector = get_connector(target.source_system)

        try:
            raw_ids: dict[int, int] = {}
            for page in fetch.received_pages:
                stored = store_body(
                    self.raw_root, target.token, page.fetched_at, scan_id, page.body,
                    target.source_system, page.sequence_no,
                )  # fmt: skip
                raw_ids[page.sequence_no] = self.conn.execute(
                    "INSERT INTO raw_responses (board_scan_id, sequence_no, request_url, "
                    "http_status, fetched_at, storage_uri, content_sha256, byte_size, "
                    "response_headers) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "RETURNING raw_response_id",
                    (
                        scan_id,
                        page.sequence_no,
                        page.request_url,
                        page.status_code,
                        page.fetched_at,
                        stored.storage_uri,
                        stored.sha256,
                        stored.byte_size,
                        Jsonb(page.headers),
                    ),
                ).fetchone()[0]

            parsed = connector.parse(saved_pages(fetch))
            with self.conn.transaction():
                counts = StoreCounts()
                if parsed.outcome != "failed":
                    counts = store_scan_results(
                        self.conn,
                        target.board_id,
                        scan_id,
                        raw_ids,
                        parsed,
                        observed_at=fetch.collected_at,
                        processed_at=self.clock(),
                    )
                self._finish_scan(scan_id, parsed, counts)
                event = incidents.record_outcome(
                    self.conn,
                    target.board_id,
                    scan_id,
                    parsed.outcome,
                    parsed.error,
                    kind,
                    self.clock(),
                    fetch.retry_after_seconds,
                )
        except Exception as exc:  # noqa: BLE001 - one board must never stop the others
            error = f"internal error while processing: {type(exc).__name__}: {exc}"[:1000]
            parsed = ParsedResponse("failed", error=error)
            counts = StoreCounts()
            with self.conn.transaction():
                self._finish_scan(scan_id, parsed, counts)
                event = incidents.record_outcome(
                    self.conn, target.board_id, scan_id, "failed", error, kind, self.clock()
                )

        outcome.status = parsed.outcome
        outcome.error = parsed.error
        outcome.reported = parsed.reported_total
        outcome.received = parsed.received
        outcome.invalid = len(parsed.rejected) if parsed.outcome != "failed" else None
        outcome.counts = counts
        outcome.incident_event = event.kind
        outcome.incident_id = event.incident_id
        return outcome

    def _finish_scan(self, scan_id: int, parsed: ParsedResponse, counts: StoreCounts) -> None:
        usable = parsed.outcome != "failed"
        self.conn.execute(
            """
            UPDATE board_scans SET status = %s, finished_at = %s, error_message = %s,
                postings_seen = %s, jobs_reported = %s, jobs_received = %s, jobs_invalid = %s,
                jobs_retained = %s, jobs_excluded = %s, jobs_needing_review = %s,
                jobs_new = %s, jobs_changed = %s, jobs_unchanged = %s
            WHERE board_scan_id = %s
            """,
            (
                parsed.outcome,
                self.clock(),
                parsed.error,
                len(parsed.jobs) if usable else None,
                parsed.reported_total,
                parsed.received,
                len(parsed.rejected) if usable else None,
                counts.retained if usable else None,
                counts.excluded if usable else None,
                counts.needing_review if usable else None,
                counts.new_postings if usable else None,
                counts.changed if usable else None,
                counts.unchanged if usable else None,
                scan_id,
            ),
        )


def summarize(report: RunReport) -> list[str]:
    """Human-readable lines for the CLI."""
    if report.run_id is None:
        lines = ["Nothing to collect."]
    else:
        lines = [f"Run {report.run_id} ({report.trigger}): {report.status}"]
    if report.abandoned_scans_recovered:
        lines.append(
            f"Recovered {report.abandoned_scans_recovered} abandoned scan(s) "
            "from a stopped run (marked failed)."
        )
    header = (
        f"{'employer':32} {'source':10} {'token':26} {'status':8} {'attempt':7} "
        f"{'pages':>5} {'reported':>8} "
        f"{'received':>8} {'invalid':>7} {'retained':>8} {'excluded':>8} {'review':>6} "
        f"{'new':>4} {'changed':>7} {'same':>5}"
    )
    if report.outcomes:
        lines.append(header)
    for o in report.outcomes:

        def n(value):
            return "-" if value is None else str(value)

        usable = o.status != "failed"
        lines.append(
            f"{o.employer[:32]:32} {o.source_system:10} {o.token[:26]:26} {o.status:8} "
            f"{o.attempt_kind:7} {n(o.pages):>5} "
            f"{n(o.reported):>8} {n(o.received):>8} {n(o.invalid):>7} "
            f"{n(o.counts.retained if usable else None):>8} "
            f"{n(o.counts.excluded if usable else None):>8} "
            f"{n(o.counts.needing_review if usable else None):>6} "
            f"{n(o.counts.new_postings if usable else None):>4} "
            f"{n(o.counts.changed if usable else None):>7} "
            f"{n(o.counts.unchanged if usable else None):>5}"
        )
        if o.error:
            lines.append(f"    {o.status}: {o.error}")
        if o.incident_event not in ("none",):
            lines.append(f"    incident: {o.incident_event.replace('_', ' ')}")
    if report.duplicate_flags_added:
        lines.append(f"Possible duplicates flagged (not merged): {report.duplicate_flags_added}")
    if report.notifications:
        lines.append(
            f"Queued {len(report.notifications)} notification(s) in the outbox "
            "(see `jobmarket notifications`)."
        )
    return lines
