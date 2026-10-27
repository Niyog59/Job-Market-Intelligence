"""Board registries: load and validate the TOML files, sync them into the database.

There is one registry file per source system (config/greenhouse_boards.toml,
config/lever_boards.toml). A file names its source with a top-level `source` key; files
written before Lever was added have none and are Greenhouse files.
"""

import os
import re
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psycopg

from jobmarket.connectors import SOURCE_SYSTEMS, get_connector

DEFAULT_REGISTRY_PATH = Path("config/greenhouse_boards.toml")
DEFAULT_REGISTRY_PATHS = (DEFAULT_REGISTRY_PATH, Path("config/lever_boards.toml"))
# Comma-separated list of registry files, replacing the defaults.
REGISTRY_PATH_ENV = "JOBMARKET_REGISTRY"

_SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
_TOKEN = re.compile(r"[a-z0-9][a-z0-9-]*")
_METROS = {"dfw", "houston"}


class RegistryError(Exception):
    """Raised when a registry file is missing or invalid."""


@dataclass(frozen=True)
class BoardEntry:
    employer: str
    employer_slug: str
    token: str
    region: str
    enabled: bool
    verification: str
    metros: tuple[str, ...]
    local_evidence: str
    official_source: str
    evidence: str
    checks: str
    group: str | None
    source_system: str = "greenhouse"
    # Hiring brands served by this one board (for example BoxLunch and Hot Topic). One
    # board is scanned once, however many brands it carries.
    brands: tuple[str, ...] = ()

    @property
    def board_url(self) -> str:
        return get_connector(self.source_system).board_url(self.token, self.region)


@dataclass(frozen=True)
class Lead:
    employer: str
    tokens: tuple[str, ...]
    status: str  # "lead" or "hold"
    notes: str
    region: str
    source_system: str = "greenhouse"


@dataclass(frozen=True)
class Registry:
    boards: tuple[BoardEntry, ...]
    leads: tuple[Lead, ...]

    @property
    def enabled(self) -> tuple[BoardEntry, ...]:
        return tuple(b for b in self.boards if b.enabled)

    @property
    def source_systems(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(b.source_system for b in self.boards))


def registry_paths() -> tuple[Path, ...]:
    value = os.environ.get(REGISTRY_PATH_ENV)
    if value:
        return tuple(Path(p.strip()) for p in value.split(",") if p.strip())
    return DEFAULT_REGISTRY_PATHS


def registry_path() -> Path:
    """The first registry file (kept for callers that show one path)."""
    return registry_paths()[0]


def load_registries(paths: tuple[Path, ...] | None = None) -> Registry:
    """Load and validate several registry files as one registry."""
    paths = registry_paths() if paths is None else paths
    problems: list[str] = []
    parts = [_load_file(path, problems) for path in paths]
    boards = tuple(b for reg in parts for b in reg.boards)
    leads = tuple(lead for reg in parts for lead in reg.leads)
    problems += _cross_checks(list(boards), list(leads))
    if problems:
        raise RegistryError("invalid registry: " + "; ".join(problems))
    return Registry(boards=boards, leads=leads)


def load_registry(path: Path | None = None) -> Registry:
    """Load one registry file, or every configured file when no path is given."""
    if path is None:
        return load_registries()
    return load_registries((path,))


def _load_file(path: Path, problems: list[str]) -> Registry:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RegistryError(f"registry file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise RegistryError(f"registry file {path} is not valid TOML: {exc}") from exc

    source = str(data.get("source", "greenhouse"))
    if source not in SOURCE_SYSTEMS:
        raise RegistryError(f"{path}: source must be one of {list(SOURCE_SYSTEMS)}")
    boards = [
        _board(raw, i, source, path.name, problems) for i, raw in enumerate(data.get("boards", []))
    ]
    leads = [_lead(raw, "lead", source) for raw in data.get("leads", [])]
    leads += [_lead(raw, "hold", source) for raw in data.get("holds", [])]
    return Registry(boards=tuple(b for b in boards if b), leads=tuple(leads))


def _cross_checks(boards: list[BoardEntry], leads: list[Lead]) -> list[str]:
    problems = []
    keys = [(b.source_system, b.token) for b in boards]
    for source, token in sorted({k for k in keys if keys.count(k) > 1}):
        problems.append(f"{source} token {token!r} appears more than once")
    names_by_slug: dict[str, set[str]] = {}
    for b in boards:
        names_by_slug.setdefault(b.employer_slug, set()).add(b.employer)
    for slug, names in names_by_slug.items():
        if len(names) > 1:
            problems.append(f"employer_slug {slug!r} has several names: {sorted(names)}")
    board_keys = set(keys)
    for lead in leads:
        for token in lead.tokens:
            if (lead.source_system, token) in board_keys:
                problems.append(f"{lead.status} token {token!r} is also listed as a board")
    return problems


def _board(
    raw: dict, index: int, source: str, filename: str, problems: list[str]
) -> BoardEntry | None:
    where = f"{filename} boards[{index}] ({raw.get('token', '?')})"
    required = ("employer", "employer_slug", "token", "region", "enabled", "verification")
    missing = [k for k in required if k not in raw]
    if missing:
        problems.append(f"{where}: missing {', '.join(missing)}")
        return None
    entry = BoardEntry(
        employer=str(raw["employer"]).strip(),
        employer_slug=str(raw["employer_slug"]),
        token=str(raw["token"]),
        region=str(raw["region"]),
        enabled=raw["enabled"],
        verification=str(raw["verification"]),
        metros=tuple(raw.get("metros", [])),
        local_evidence=str(raw.get("local_evidence", "")),
        official_source=str(raw.get("official_source", "")),
        evidence=str(raw.get("evidence", "")),
        checks=str(raw.get("checks", "")),
        group=raw.get("group"),
        source_system=source,
        brands=tuple(str(b) for b in raw.get("brands", [])),
    )
    regions = get_connector(source).regions
    if not entry.employer:
        problems.append(f"{where}: employer is empty")
    if not _SLUG.fullmatch(entry.employer_slug):
        problems.append(f"{where}: invalid employer_slug {entry.employer_slug!r}")
    if not _TOKEN.fullmatch(entry.token):
        problems.append(f"{where}: invalid token {entry.token!r}")
    if entry.region not in regions:
        problems.append(f"{where}: region must be one of {list(regions)} for {source}")
    if not isinstance(entry.enabled, bool):
        problems.append(f"{where}: enabled must be true or false")
    if entry.verification not in ("verified", "pending"):
        problems.append(f"{where}: verification must be 'verified' or 'pending'")
    if entry.enabled is True and entry.verification != "verified":
        problems.append(f"{where}: only verified boards can be enabled")
    if entry.enabled is True and not entry.official_source.startswith("https://"):
        problems.append(f"{where}: enabled boards need an https official_source")
    unknown = set(entry.metros) - _METROS
    if unknown:
        problems.append(f"{where}: metros must be a subset of {sorted(_METROS)}")
    if entry.enabled is True and not entry.metros:
        # A pending board may honestly have no local evidence yet; an enabled one may not.
        problems.append(f"{where}: enabled boards need at least one metro")
    return entry


def _lead(raw: dict, status: str, source: str) -> Lead:
    return Lead(
        employer=str(raw.get("employer", "")),
        tokens=tuple(raw.get("tokens", [])),
        status=status,
        notes=str(raw.get("notes", "")),
        region=str(raw.get("region", "us" if source == "greenhouse" else "global")),
        source_system=source,
    )


@dataclass(frozen=True)
class SyncResult:
    employers_upserted: int
    boards_upserted: int
    boards_enabled: int
    boards_not_in_registry: tuple[str, ...]


def sync_registry(conn: psycopg.Connection, registry: Registry, now: datetime) -> SyncResult:
    """Insert or update employers and boards to match the registry, in one transaction.

    Boards of a loaded source system that are in the database but no longer in the
    registry are disabled, never deleted, so their history stays intact. Boards of source
    systems the registry does not cover are left alone.
    """
    with conn.transaction():
        employer_ids: dict[str, int] = {}
        for b in registry.boards:
            if b.employer_slug in employer_ids:
                continue
            employer_ids[b.employer_slug] = conn.execute(
                """
                INSERT INTO employers (name, slug, website_url, corporate_group)
                VALUES (%s, %s, NULLIF(%s, ''), %s)
                ON CONFLICT (slug) DO UPDATE
                    SET name = EXCLUDED.name,
                        corporate_group = EXCLUDED.corporate_group
                RETURNING employer_id
                """,
                (b.employer, b.employer_slug, "", b.group),
            ).fetchone()[0]

        for b in registry.boards:
            conn.execute(
                """
                INSERT INTO source_boards (
                    employer_id, source_system, board_key, board_url, is_active,
                    hosting_region, verification_status, metros, local_evidence,
                    official_source_url, evidence_notes, special_checks, hiring_brands,
                    registry_synced_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                        NULLIF(%s, ''), NULLIF(%s, ''), NULLIF(%s, ''), %s, %s)
                ON CONFLICT (source_system, board_key) DO UPDATE SET
                    employer_id = EXCLUDED.employer_id,
                    board_url = EXCLUDED.board_url,
                    is_active = EXCLUDED.is_active,
                    hosting_region = EXCLUDED.hosting_region,
                    verification_status = EXCLUDED.verification_status,
                    metros = EXCLUDED.metros,
                    local_evidence = EXCLUDED.local_evidence,
                    official_source_url = EXCLUDED.official_source_url,
                    evidence_notes = EXCLUDED.evidence_notes,
                    special_checks = EXCLUDED.special_checks,
                    hiring_brands = EXCLUDED.hiring_brands,
                    registry_synced_at = EXCLUDED.registry_synced_at
                """,
                (
                    employer_ids[b.employer_slug],
                    b.source_system,
                    b.token,
                    b.board_url,
                    b.enabled,
                    b.region,
                    b.verification,
                    list(b.metros),
                    b.local_evidence,
                    b.official_source,
                    b.evidence,
                    b.checks,
                    list(b.brands),
                    now,
                ),
            )

        stale = []
        for source in registry.source_systems:
            tokens = [b.token for b in registry.boards if b.source_system == source]
            stale += conn.execute(
                """
                UPDATE source_boards SET is_active = false
                WHERE source_system = %s AND NOT (board_key = ANY(%s)) AND is_active
                RETURNING board_key
                """,
                (source, tokens),
            ).fetchall()

    return SyncResult(
        employers_upserted=len(employer_ids),
        boards_upserted=len(registry.boards),
        boards_enabled=len(registry.enabled),
        boards_not_in_registry=tuple(sorted(t for (t,) in stale)),
    )
