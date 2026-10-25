"""Forward-only SQL migrations.

Migrations are files named ``NNNN_description.sql`` and are applied in version order.
Each file runs in its own transaction together with its row in ``schema_migrations``, so
a failing file leaves no partial changes and is retried on the next run. Files that
have been applied must never be edited; fixes go in a new migration. The runner checks
this with a SHA-256 checksum of each file.

Migration files must not contain transaction control (BEGIN/COMMIT) or statements that
cannot run inside a transaction, such as CREATE INDEX CONCURRENTLY.
"""

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

import psycopg

MIGRATION_FILENAME = re.compile(r"(?P<version>[0-9]{4})_[a-z0-9_]+\.sql")

# Arbitrary application-wide key for pg_advisory_lock, so two runners cannot apply the
# same migrations concurrently.
ADVISORY_LOCK_KEY = 4_815_162_342

CREATE_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    text PRIMARY KEY,
    version     integer NOT NULL UNIQUE,
    checksum    text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationError(Exception):
    """Raised when migrations are inconsistent or one fails to apply."""


@dataclass(frozen=True)
class Migration:
    version: int
    filename: str
    sql: str
    checksum: str


@dataclass(frozen=True)
class MigrationStatus:
    filename: str
    applied_at: datetime | None


def default_directory() -> Traversable:
    return resources.files("jobmarket") / "migrations"


def discover(directory: Traversable | Path | None = None) -> list[Migration]:
    """Load migration files from ``directory`` (the packaged migrations by default).

    Files not ending in ``.sql`` are ignored. Raises MigrationError for a ``.sql`` file
    with an invalid name or for two files sharing a version number.
    """
    directory = default_directory() if directory is None else directory
    by_version: dict[int, Migration] = {}
    for entry in directory.iterdir():
        if not entry.is_file() or not entry.name.endswith(".sql"):
            continue
        match = MIGRATION_FILENAME.fullmatch(entry.name)
        if match is None:
            raise MigrationError(
                f"invalid migration filename {entry.name!r}; expected NNNN_description.sql "
                "(lowercase letters, digits, underscores)"
            )
        version = int(match["version"])
        if version in by_version:
            raise MigrationError(
                f"duplicate migration version {version:04d}: "
                f"{by_version[version].filename!r} and {entry.name!r}"
            )
        content = entry.read_bytes()
        by_version[version] = Migration(
            version=version,
            filename=entry.name,
            sql=content.decode("utf-8"),
            checksum=hashlib.sha256(content).hexdigest(),
        )
    return [by_version[v] for v in sorted(by_version)]


def _applied(conn: psycopg.Connection) -> dict[str, tuple[int, str, datetime]]:
    rows = conn.execute(
        "SELECT filename, version, checksum, applied_at FROM schema_migrations"
    ).fetchall()
    return {
        filename: (version, checksum, applied_at)
        for filename, version, checksum, applied_at in rows
    }


def _pending(
    migrations: list[Migration], applied: dict[str, tuple[int, str, datetime]]
) -> list[Migration]:
    """Validate applied history against the files and return what still needs applying."""
    known = {m.filename: m for m in migrations}

    for filename, (_, checksum, _) in sorted(applied.items()):
        migration = known.get(filename)
        if migration is None:
            raise MigrationError(
                f"migration {filename!r} is recorded as applied but its file is missing"
            )
        if migration.checksum != checksum:
            raise MigrationError(
                f"migration {filename!r} was modified after it was applied; "
                "add a new migration instead of editing an applied one"
            )

    pending = [m for m in migrations if m.filename not in applied]
    latest_applied = max((version for version, _, _ in applied.values()), default=0)
    out_of_order = [m.filename for m in pending if m.version < latest_applied]
    if out_of_order:
        raise MigrationError(
            f"migrations {out_of_order} are older than the latest applied migration "
            f"({latest_applied:04d}); renumber them after it"
        )
    return pending


def _require_autocommit(conn: psycopg.Connection) -> None:
    # Each migration needs its own real transaction. On a non-autocommit connection,
    # conn.transaction() could become a savepoint inside the caller's open transaction.
    if not conn.autocommit:
        raise ValueError("migrations need a connection with autocommit=True")


def apply_migrations(
    conn: psycopg.Connection, migrations: Iterable[Migration] | None = None
) -> list[str]:
    """Apply every pending migration and return the filenames applied, in order.

    Unqualified table names, including ``schema_migrations``, resolve through the
    connection's search_path. Migrations applied before a failure stay applied.
    """
    _require_autocommit(conn)
    migrations = discover() if migrations is None else list(migrations)

    conn.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
    try:
        conn.execute(CREATE_TRACKING_TABLE)
        pending = _pending(migrations, _applied(conn))
        applied_now: list[str] = []
        for migration in pending:
            try:
                with conn.transaction():
                    conn.execute(migration.sql)
                    conn.execute(
                        "INSERT INTO schema_migrations (filename, version, checksum) "
                        "VALUES (%s, %s, %s)",
                        (migration.filename, migration.version, migration.checksum),
                    )
            except psycopg.Error as exc:
                raise MigrationError(f"migration {migration.filename!r} failed: {exc}") from exc
            applied_now.append(migration.filename)
        return applied_now
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))


def migration_status(
    conn: psycopg.Connection, migrations: Iterable[Migration] | None = None
) -> list[MigrationStatus]:
    """Return every known migration with its applied time (None if pending).

    Raises MigrationError if the applied history and the files disagree. Makes no
    changes when the tracking table does not exist yet.
    """
    migrations = discover() if migrations is None else list(migrations)
    exists = conn.execute("SELECT to_regclass('schema_migrations') IS NOT NULL").fetchone()[0]
    applied = _applied(conn) if exists else {}
    _pending(migrations, applied)
    return [
        MigrationStatus(m.filename, applied[m.filename][2] if m.filename in applied else None)
        for m in migrations
    ]
