"""Raw source responses on local disk, with a fixed retention period.

The database row for each response (URL, status, fingerprint, size) is permanent; only
the stored bytes are deleted after RAW_RETENTION. Job records and history are never
touched by cleanup.
"""

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import psycopg

from jobmarket.connectors import SOURCE_SYSTEMS

RAW_DIR_ENV = "JOBMARKET_RAW_DIR"
DEFAULT_RAW_DIR = Path("data/raw")
RAW_RETENTION = timedelta(days=4)


def raw_dir() -> Path:
    return Path(os.environ.get(RAW_DIR_ENV, DEFAULT_RAW_DIR))


@dataclass(frozen=True)
class StoredBody:
    storage_uri: str
    sha256: str
    byte_size: int


def store_body(
    root: Path,
    token: str,
    fetched_at: datetime,
    scan_id: int,
    body: bytes,
    source_system: str = "greenhouse",
    sequence_no: int = 1,
) -> StoredBody:
    """Write one response page under root/<source>/<token>/ and return a path relative to
    root. Later pages of a paginated scan get a -p<N> suffix.

    Written to a temporary name first and renamed, so a crash never leaves a half-written
    file under the final name.
    """
    stamp = fetched_at.strftime("%Y%m%dT%H%M%S%fZ")
    suffix = "" if sequence_no == 1 else f"-p{sequence_no}"
    relative = Path(source_system) / token / f"{stamp}-scan{scan_id}{suffix}.json"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(body)
    tmp.replace(target)
    return StoredBody(relative.as_posix(), hashlib.sha256(body).hexdigest(), len(body))


@dataclass(frozen=True)
class CleanupResult:
    purged_rows: int
    files_deleted: int
    files_already_missing: int
    orphan_files_deleted: int
    dry_run: bool


def cleanup_raw(
    conn: psycopg.Connection,
    root: Path,
    now: datetime,
    retention: timedelta = RAW_RETENTION,
    dry_run: bool = False,
) -> CleanupResult:
    """Delete stored response bytes older than the retention period.

    Rows in raw_responses are kept and marked with purged_at. Files on disk with no row
    (left behind if a process stopped between writing a file and recording it) are also
    deleted once they are older than the retention period.
    """
    cutoff = now - retention
    rows = conn.execute(
        "SELECT raw_response_id, storage_uri FROM raw_responses "
        "WHERE purged_at IS NULL AND fetched_at < %s ORDER BY raw_response_id",
        (cutoff,),
    ).fetchall()

    deleted = missing = 0
    purged_ids = []
    for raw_id, uri in rows:
        path = root / uri
        if path.exists():
            deleted += 1
            if not dry_run:
                # A crash after this line leaves the row unmarked; the next cleanup sees
                # the file missing and marks it then.
                path.unlink()
        else:
            missing += 1
        purged_ids.append(raw_id)
    if not dry_run and purged_ids:
        with conn.transaction():
            conn.execute(
                "UPDATE raw_responses SET purged_at = %s WHERE raw_response_id = ANY(%s)",
                (now, purged_ids),
            )

    known = {uri for (uri,) in conn.execute("SELECT storage_uri FROM raw_responses")}
    orphans = 0
    for base in (root / source for source in SOURCE_SYSTEMS):
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=now.tzinfo)
            if relative not in known and modified < cutoff:
                orphans += 1
                if not dry_run:
                    path.unlink()

    return CleanupResult(len(rows), deleted, missing, orphans, dry_run)
