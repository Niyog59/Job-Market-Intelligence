"""Database backups and restores with the PostgreSQL client tools.

A backup is a directory named by its UTC time (data/backups/20261101T060000Z/) holding:

    ingestion.dump   pg_dump of the public schema (jobs, history, extractions, migrations)
    private.dump     pg_dump of the private schema (owner account, profile, saved jobs)
    manifest.json    sizes, SHA-256 checksums, server and pg_dump versions, migration state

The analytics schemas are not backed up: `jobmarket analytics build` recreates them from the
ingestion data. Dumps use the custom format without owners or privileges, so they restore
into a server whose roles were created fresh; the grants are applied again afterwards with
`jobmarket analytics grant` and `jobmarket private grant`. Roles and their passwords live in
the server, not in any dump, and are never written to a backup.

The directory is written under a ".partial" name and renamed when complete, so a failed
backup never looks finished. Restore writes only into an empty database and checks every
checksum first.

The database password reaches pg_dump and pg_restore through the PGPASSWORD environment
variable of the child process, never on a command line.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from jobmarket import db
from jobmarket.config import Settings
from jobmarket.s3store import BACKUP_PREFIXES, S3Store

PG_BIN_ENV = "JOBMARKET_PG_BIN"
DEFAULT_BACKUP_DIR = Path("data/backups")
MANIFEST = "manifest.json"
UPLOADED = "UPLOADED"  # written only after S3 confirms every object of the backup
FORMAT_VERSION = 1
# Backup part -> (schema, file name). Separate files so the private part can be stored and
# protected apart from the job data.
PARTS = {"ingestion": ("public", "ingestion.dump"), "private": ("private", "private.dump")}
_STAMP = re.compile(r"[0-9]{8}T[0-9]{6}Z")
# pg_restore list entries that create or comment on the public schema, which every new
# database already has (restoring them would fail, or need ownership of that schema).
_PUBLIC_SCHEMA_ENTRY = re.compile(r"\b(SCHEMA - public|COMMENT - SCHEMA public)\b")


class BackupError(Exception):
    """A backup or restore could not be made or verified."""


def tool(name: str) -> str:
    """Path of a PostgreSQL client program (JOBMARKET_PG_BIN, else the PATH)."""
    directory = os.environ.get(PG_BIN_ENV, "").strip()
    path = str(Path(directory) / name) if directory else shutil.which(name)
    if not path or not Path(path).exists():
        raise BackupError(
            f"{name} not found. Install the PostgreSQL 17 client tools or set {PG_BIN_ENV} "
            "to the directory that contains them."
        )
    return path


def _pg_env(settings: Settings) -> dict[str, str]:
    env = dict(os.environ)
    env.update(PGHOST=settings.db_host, PGPORT=str(settings.db_port),
               PGDATABASE=settings.db_name, PGUSER=settings.db_user)  # fmt: skip
    if settings.db_password:
        env["PGPASSWORD"] = settings.db_password
    else:
        env.pop("PGPASSWORD", None)
    return env


def _run(args: list[str], env: dict[str, str]) -> str:
    done = subprocess.run(args, env=env, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise BackupError(f"{Path(args[0]).name} failed: {done.stderr.strip()}")
    return done.stdout


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def stamp_for(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def create_backup(settings: Settings, root: Path, now: datetime) -> Path:
    """Dump the public and private schemas into root/<stamp>/ and return that directory."""
    pg_dump = tool("pg_dump")
    stamp = stamp_for(now)
    final = root / stamp
    partial = root / f"{stamp}.partial"
    if final.exists() or partial.exists():
        raise BackupError(f"{final} already exists")
    with db.connect(settings) as conn:
        server_version = conn.execute("SHOW server_version").fetchone()[0]
        count, latest = conn.execute(
            "SELECT count(*), max(filename) FROM public.schema_migrations"
        ).fetchone()
        missing = [
            schema
            for schema, _ in PARTS.values()
            if conn.execute("SELECT to_regnamespace(%s)", (schema,)).fetchone()[0] is None
        ]
    if missing:
        raise BackupError(f"schema not found: {', '.join(missing)} (run `jobmarket migrate`)")

    env = _pg_env(settings)
    partial.mkdir(parents=True)
    try:
        parts = {}
        for part, (schema, name) in PARTS.items():
            path = partial / name
            _run([pg_dump, "--format=custom", f"--schema={schema}", "--no-owner",
                  "--no-privileges", f"--file={path}"], env)  # fmt: skip
            parts[part] = {"schema": schema, "file": name, "bytes": path.stat().st_size,
                           "sha256": _sha256(path)}  # fmt: skip
        manifest = {
            "format_version": FORMAT_VERSION,
            "stamp": stamp,
            "created_at": now.astimezone(UTC).isoformat(),
            "database": settings.db_name,
            "server_version": server_version,
            "pg_dump_version": _run([pg_dump, "--version"], env).strip(),
            "migrations": {"applied": count, "latest": latest},
            "parts": parts,
        }
        (partial / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
        partial.rename(final)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return final


def verify_backup(directory: Path) -> dict:
    """The manifest, after checking that every dump is present with its recorded checksum."""
    path = directory / MANIFEST
    if not path.exists():
        raise BackupError(f"{path} not found; not a complete backup")
    manifest = json.loads(path.read_text())
    if manifest.get("format_version") != FORMAT_VERSION:
        raise BackupError(f"unsupported backup format {manifest.get('format_version')!r}")
    if set(manifest.get("parts", {})) != set(PARTS):
        raise BackupError(f"the backup must contain {' and '.join(PARTS)}")
    for part, info in manifest["parts"].items():
        dump = directory / info["file"]
        if not dump.exists():
            raise BackupError(f"{dump} is missing")
        if dump.stat().st_size != info["bytes"] or _sha256(dump) != info["sha256"]:
            raise BackupError(f"{dump} does not match its checksum; the {part} part is damaged")
    return manifest


def upload_backup(store: S3Store, directory: Path, now: datetime | None = None) -> list[str]:
    """Upload a verified backup: each dump under its own prefix, the manifest last. Then
    confirm every object is in S3 with the local size, and only then write UPLOADED in the
    backup directory (the mark pruning relies on)."""
    manifest = verify_backup(directory)
    keys = []
    for part, info in manifest["parts"].items():
        prefix = f"{BACKUP_PREFIXES[part]}{manifest['stamp']}/"
        for name in (info["file"], MANIFEST):
            store.upload(directory / name, prefix + name)
            keys.append((prefix + name, (directory / name).stat().st_size))
    for key, size in keys:
        stored = store.size(key)
        if stored != size:
            raise BackupError(f"S3 has {key} with {stored} bytes, expected {size}")
    (directory / UPLOADED).write_text(json.dumps({
        "confirmed_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
        "bucket": store.bucket,
        "objects": {key: size for key, size in keys},
    }, indent=2) + "\n")  # fmt: skip
    return [key for key, _ in keys]


def is_uploaded(directory: Path) -> bool:
    return (directory / UPLOADED).exists()


@dataclass(frozen=True)
class PruneResult:
    kept: list[str]
    deleted: list[str]
    kept_not_uploaded: list[str]  # older than the newest ones, but not confirmed in S3
    damaged: list[str]  # fail verification; never deleted automatically
    partials_removed: list[str]


def prune_backups(root: Path, keep: int) -> PruneResult:
    """Bound the local backups: keep the newest `keep` verified backups; delete an older
    verified one only if its S3 upload was confirmed. Damaged backups are reported, never
    deleted. Unfinished "<stamp>.partial" directories (an interrupted backup) are removed.

    Run it only when no backup is being written (the pipeline runs one step at a time).
    """
    if keep < 1:
        raise ValueError("keep at least one verified backup")
    partials = sorted(p for p in root.glob("*.partial") if p.is_dir())
    for path in partials:
        shutil.rmtree(path)
    verified, damaged = [], []
    for path in sorted((p for p in root.iterdir() if p.is_dir() and _STAMP.fullmatch(p.name)),
                       reverse=True) if root.exists() else []:  # fmt: skip
        try:
            verify_backup(path)
            verified.append(path)
        except (BackupError, ValueError, KeyError):
            damaged.append(path.name)
    kept = [p.name for p in verified[:keep]]
    deleted, kept_not_uploaded = [], []
    for path in verified[keep:]:
        if is_uploaded(path):
            shutil.rmtree(path)
            deleted.append(path.name)
        else:
            kept_not_uploaded.append(path.name)
    return PruneResult(kept, deleted, kept_not_uploaded, damaged, [p.name for p in partials])


def list_backups(store: S3Store) -> dict[str, list[str]]:
    """Backups in S3 by stamp, with the parts whose upload finished (manifest present)."""
    found: dict[str, list[str]] = {}
    for part, prefix in BACKUP_PREFIXES.items():
        for key in store.keys(prefix):
            stamp, _, name = key[len(prefix) :].partition("/")
            if name == MANIFEST and _STAMP.fullmatch(stamp):
                found.setdefault(stamp, []).append(part)
    return dict(sorted(found.items()))


def fetch_backup(store: S3Store, stamp: str, root: Path) -> Path:
    """Download one backup from S3 into root/<stamp>/ and verify it."""
    if not _STAMP.fullmatch(stamp):
        raise BackupError(f"not a backup name: {stamp!r} (expected like 20261101T060000Z)")
    target = root / stamp
    if target.exists():
        raise BackupError(f"{target} already exists")
    for part, (_, name) in PARTS.items():
        prefix = f"{BACKUP_PREFIXES[part]}{stamp}/"
        store.download(prefix + name, target / name)
        store.download(prefix + MANIFEST, target / MANIFEST)
    verify_backup(target)
    return target


@dataclass(frozen=True)
class RestoreResult:
    database: str
    migrations_applied: int
    migrations_expected: int
    postings: int
    owner_accounts: int


def _existing_objects(conn) -> tuple[bool, list[str]]:
    """Whether the public schema exists, and what makes the database not empty."""
    schemas = [name for (name,) in conn.execute(
        "SELECT nspname FROM pg_namespace WHERE nspname NOT IN ('pg_catalog', "
        "'information_schema') AND nspname NOT LIKE 'pg\\_%'")]  # fmt: skip
    problems = [f"schema {name}" for name in schemas if name != "public"]
    if "public" in schemas:
        relations = conn.execute(
            "SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace"
        ).fetchone()[0]
        functions = conn.execute(
            "SELECT count(*) FROM pg_proc WHERE pronamespace = 'public'::regnamespace"
        ).fetchone()[0]
        if relations or functions:
            problems.append(f"{relations} tables or other relations and {functions} functions "
                            "in schema public")  # fmt: skip
    return "public" in schemas, problems


def restore_backup(target: Settings, directory: Path) -> RestoreResult:
    """Restore a verified backup into target.db_name, which must be an empty database.

    Each part is restored in one transaction (all or nothing). Objects become owned by the
    target login. Grants are not restored; apply them afterwards.
    """
    with db.connect(target, autocommit=True) as conn:
        public_exists, problems = _existing_objects(conn)
    if problems:
        raise BackupError(f"database {target.db_name} is not empty ({'; '.join(problems)}). "
                          "Restore only into a new, empty database.")  # fmt: skip
    manifest = verify_backup(directory)
    pg_restore = tool("pg_restore")

    env = _pg_env(target)
    with tempfile.TemporaryDirectory() as scratch:
        for part in PARTS:
            dump = directory / manifest["parts"][part]["file"]
            entries = _run([pg_restore, "--list", str(dump)], env).splitlines()
            if public_exists:
                entries = [line for line in entries if not _PUBLIC_SCHEMA_ENTRY.search(line)]
            listing = Path(scratch) / f"{part}.list"
            listing.write_text("\n".join(entries) + "\n")
            _run([pg_restore, "--exit-on-error", "--single-transaction", "--no-owner",
                  "--no-privileges", f"--use-list={listing}", f"--dbname={target.db_name}",
                  str(dump)], env)  # fmt: skip

    with db.connect(target) as conn:
        applied = conn.execute("SELECT count(*) FROM public.schema_migrations").fetchone()[0]
        postings = conn.execute("SELECT count(*) FROM public.source_postings").fetchone()[0]
        owners = conn.execute("SELECT count(*) FROM private.accounts").fetchone()[0]
    return RestoreResult(target.db_name, applied, manifest["migrations"]["applied"], postings,
                         owners)  # fmt: skip
