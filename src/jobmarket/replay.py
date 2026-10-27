"""Reprocess saved raw responses under their original board scan and collection time.

A replay reads every stored page of a scan, checks each against its recorded SHA-256,
validates them again with the board's connector, and stores the result with the same
routine as a live collection, but with the original collection time and a later
processing time. A page that never arrived was never stored, so a replay of a scan that
stopped early is judged partial again, as the original was.

Because observations are unique per (board scan, posting) and current state only moves
forward in collection time, replaying a scan that was already processed adds nothing, and
replaying an older one adds history without replacing newer state.

Replay never changes the scan's recorded status, its retry incident, or alerts: those
describe what happened at collection time. It also never applies a scan's absence evidence
a second time; absence counts of the postings it touches are recomputed from scan history.

Raw bytes are kept for RAW_RETENTION. After that, the response's row and everything already
recorded from it remain, but the response itself cannot be replayed, and replay says so.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psycopg

from jobmarket.clock import Clock
from jobmarket.collector import collection_lock
from jobmarket.connectors import SavedPage, get_connector
from jobmarket.raw_store import RAW_RETENTION
from jobmarket.store import StoreCounts, store_scan_results


class UnknownScan(ValueError):
    """No board scan has the requested id."""


@dataclass
class ReplayResult:
    board_scan_id: int
    token: str
    collected_at: datetime | None
    scan_status: str
    outcome: str = "processed"
    detail: str | None = None
    validation: str | None = None
    counts: StoreCounts | None = None
    replay_id: int | None = None


def available_scan_ids(conn: psycopg.Connection) -> list[int]:
    """Scans whose raw pages are all still stored, oldest collection first."""
    rows = conn.execute(
        "SELECT board_scan_id FROM raw_responses GROUP BY board_scan_id "
        "HAVING bool_and(purged_at IS NULL) ORDER BY min(fetched_at), board_scan_id"
    ).fetchall()
    return [r[0] for r in rows]


def replay_scans(
    conn: psycopg.Connection, raw_root: Path, scan_ids: list[int], clock: Clock
) -> list[ReplayResult]:
    """Replay each scan in turn. Holds the collection lock, so it never overlaps a
    collection; raises CollectionBusy if one is running."""
    if not conn.autocommit:
        raise ValueError("replay needs an autocommit connection")
    with collection_lock(conn):
        return [_replay_one(conn, raw_root, scan_id, clock) for scan_id in scan_ids]


def _replay_one(
    conn: psycopg.Connection, raw_root: Path, scan_id: int, clock: Clock
) -> ReplayResult:
    started = clock()
    row = conn.execute(
        "SELECT s.board_id, b.board_key, b.source_system, s.status FROM board_scans s "
        "JOIN source_boards b ON b.board_id = s.board_id WHERE s.board_scan_id = %s",
        (scan_id,),
    ).fetchone()
    if row is None:
        raise UnknownScan(f"there is no board scan {scan_id}")
    board_id, token, source_system, scan_status = row
    stored = conn.execute(
        "SELECT raw_response_id, sequence_no, fetched_at, request_url, storage_uri, "
        "content_sha256, http_status, response_headers, purged_at FROM raw_responses "
        "WHERE board_scan_id = %s ORDER BY sequence_no",
        (scan_id,),
    ).fetchall()
    collected_at = stored[0][2] if stored else None
    raw_id = stored[0][0] if stored else None
    result = ReplayResult(scan_id, token, collected_at, scan_status)

    pages: list[SavedPage] | None = None
    purged = [r[8] for r in stored if r[8] is not None]
    if not stored:
        result.outcome = "no_response"
        result.detail = "no response was received in this scan, so there is nothing to replay"
    elif purged:
        result.outcome = "expired"
        result.detail = (
            f"the raw response was deleted by the {RAW_RETENTION.days}-day raw retention on "
            f"{max(purged).isoformat(timespec='minutes')} and cannot be replayed; history "
            "already recorded from it is kept"
        )
    else:
        pages = []
        for _, seq, _, url, uri, sha, status, headers, _ in stored:
            try:
                body = (raw_root / uri).read_bytes()
            except FileNotFoundError:
                result.outcome = "file_missing"
                result.detail = f"the raw response file {uri} is missing from {raw_root}"
                pages = None
                break
            if hashlib.sha256(body).hexdigest() != sha:
                result.outcome = "checksum_mismatch"
                result.detail = (
                    f"the stored file {uri} does not match its recorded SHA-256; not used"
                )
                pages = None
                break
            pages.append(SavedPage(seq, url, status, headers or {}, body))
        raw_ids = {r[1]: r[0] for r in stored}

    if pages is not None:
        parsed = get_connector(source_system).parse(pages)
        result.validation = parsed.outcome
        if parsed.outcome == "failed":
            result.outcome = "unusable"
            result.detail = f"the saved response has no usable data: {parsed.error}"
        else:
            try:
                # The store and its replay record commit together or not at all.
                with conn.transaction():
                    result.counts = store_scan_results(
                        conn,
                        board_id,
                        scan_id,
                        raw_ids,
                        parsed,
                        observed_at=collected_at,
                        processed_at=clock(),
                        recorded_by="replay",
                    )
                    result.replay_id = _record(conn, result, raw_id, started, clock())
                return result
            except Exception as exc:  # noqa: BLE001 - one scan must never stop the others
                result.outcome = "failed"
                result.counts = None
                result.detail = f"{type(exc).__name__}: {exc}"[:1000]

    with conn.transaction():
        result.replay_id = _record(conn, result, raw_id, started, clock())
    return result


def _record(
    conn: psycopg.Connection,
    result: ReplayResult,
    raw_id: int | None,
    started: datetime,
    finished: datetime,
) -> int:
    c = result.counts
    return conn.execute(
        "INSERT INTO replay_runs (board_scan_id, raw_response_id, collected_at, started_at, "
        "finished_at, outcome, validation_outcome, detail, postings_new, versions_new, "
        "observations_new, observations_existing, observations_historical) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING replay_id",
        (
            result.board_scan_id,
            raw_id,
            result.collected_at,
            started,
            finished,
            result.outcome,
            result.validation,
            result.detail,
            c.new_postings if c else None,
            c.new_versions if c else None,
            c.new_observations if c else None,
            c.already_recorded if c else None,
            c.historical if c else None,
        ),
    ).fetchone()[0]
