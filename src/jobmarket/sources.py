"""Greenhouse board registry: load and validate the TOML file, sync it into the database."""

import os
import re
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psycopg

DEFAULT_REGISTRY_PATH = Path("config/greenhouse_boards.toml")
REGISTRY_PATH_ENV = "JOBMARKET_REGISTRY"

BOARD_URL = {
    "us": "https://job-boards.greenhouse.io/{token}",
    "eu": "https://job-boards.eu.greenhouse.io/{token}",
}

_SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
_TOKEN = re.compile(r"[a-z0-9][a-z0-9-]*")
_METROS = {"dfw", "houston"}


class RegistryError(Exception):
    """Raised when the registry file is missing or invalid."""


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

    @property
    def board_url(self) -> str:
        return BOARD_URL[self.region].format(token=self.token)


@dataclass(frozen=True)
class Lead:
    employer: str
    tokens: tuple[str, ...]
    status: str  # "lead" or "hold"
    notes: str
    region: str


@dataclass(frozen=True)
class Registry:
    boards: tuple[BoardEntry, ...]
    leads: tuple[Lead, ...]

    @property
    def enabled(self) -> tuple[BoardEntry, ...]:
        return tuple(b for b in self.boards if b.enabled)


def registry_path() -> Path:
    return Path(os.environ.get(REGISTRY_PATH_ENV, DEFAULT_REGISTRY_PATH))


def load_registry(path: Path | None = None) -> Registry:
    path = registry_path() if path is None else path
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RegistryError(f"registry file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise RegistryError(f"registry file {path} is not valid TOML: {exc}") from exc

    problems: list[str] = []
    boards = [_board(raw, i, problems) for i, raw in enumerate(data.get("boards", []))]
    leads = [_lead(raw, "lead") for raw in data.get("leads", [])]
    leads += [_lead(raw, "hold") for raw in data.get("holds", [])]

    tokens = [b.token for b in boards if b]
    for token in sorted({t for t in tokens if tokens.count(t) > 1}):
        problems.append(f"token {token!r} appears more than once")
    names_by_slug: dict[str, set[str]] = {}
    for b in boards:
        if b:
            names_by_slug.setdefault(b.employer_slug, set()).add(b.employer)
    for slug, names in names_by_slug.items():
        if len(names) > 1:
            problems.append(f"employer_slug {slug!r} has several names: {sorted(names)}")
    board_tokens = set(tokens)
    for lead in leads:
        for token in lead.tokens:
            if token in board_tokens:
                problems.append(f"{lead.status} token {token!r} is also listed as a board")

    if problems:
        raise RegistryError("invalid registry: " + "; ".join(problems))
    return Registry(boards=tuple(boards), leads=tuple(leads))


def _board(raw: dict, index: int, problems: list[str]) -> BoardEntry | None:
    where = f"boards[{index}] ({raw.get('token', '?')})"
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
    )
    if not entry.employer:
        problems.append(f"{where}: employer is empty")
    if not _SLUG.fullmatch(entry.employer_slug):
        problems.append(f"{where}: invalid employer_slug {entry.employer_slug!r}")
    if not _TOKEN.fullmatch(entry.token):
        problems.append(f"{where}: invalid token {entry.token!r}")
    if entry.region not in BOARD_URL:
        problems.append(f"{where}: region must be 'us' or 'eu'")
    if not isinstance(entry.enabled, bool):
        problems.append(f"{where}: enabled must be true or false")
    if entry.verification not in ("verified", "pending"):
        problems.append(f"{where}: verification must be 'verified' or 'pending'")
    if entry.enabled is True and entry.verification != "verified":
        problems.append(f"{where}: only verified boards can be enabled")
    if entry.enabled is True and not entry.official_source.startswith("https://"):
        problems.append(f"{where}: enabled boards need an https official_source")
    unknown = set(entry.metros) - _METROS
    if unknown or not entry.metros:
        problems.append(f"{where}: metros must be a non-empty subset of {sorted(_METROS)}")
    return entry


def _lead(raw: dict, status: str) -> Lead:
    return Lead(
        employer=str(raw.get("employer", "")),
        tokens=tuple(raw.get("tokens", [])),
        status=status,
        notes=str(raw.get("notes", "")),
        region=str(raw.get("region", "us")),
    )


@dataclass(frozen=True)
class SyncResult:
    employers_upserted: int
    boards_upserted: int
    boards_enabled: int
    boards_not_in_registry: tuple[str, ...]


def sync_registry(conn: psycopg.Connection, registry: Registry, now: datetime) -> SyncResult:
    """Insert or update employers and boards to match the registry, in one transaction.

    Boards that are in the database but no longer in the registry are disabled, never
    deleted, so their history stays intact.
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
                    official_source_url, evidence_notes, special_checks, registry_synced_at)
                VALUES (%s, 'greenhouse', %s, %s, %s, %s, %s, %s, %s,
                        NULLIF(%s, ''), NULLIF(%s, ''), NULLIF(%s, ''), %s)
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
                    registry_synced_at = EXCLUDED.registry_synced_at
                """,
                (
                    employer_ids[b.employer_slug],
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
                    now,
                ),
            )

        tokens = [b.token for b in registry.boards]
        stale = conn.execute(
            """
            UPDATE source_boards SET is_active = false
            WHERE source_system = 'greenhouse' AND NOT (board_key = ANY(%s)) AND is_active
            RETURNING board_key
            """,
            (tokens,),
        ).fetchall()

    return SyncResult(
        employers_upserted=len(employer_ids),
        boards_upserted=len(registry.boards),
        boards_enabled=len(registry.enabled),
        boards_not_in_registry=tuple(sorted(t for (t,) in stale)),
    )
