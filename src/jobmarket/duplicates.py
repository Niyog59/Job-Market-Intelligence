"""Cross-record duplicate handling: strong groups and possible-duplicate flags.

Repeated ingestion of one source record is already impossible (identity is board + source job
id, unique per board). This module compares *different* source records, possibly from
different boards or sources, whose source ids are not comparable with each other.

Strong evidence groups records automatically (duplicate_groups):
  requisition_id   same employer and the same employer-issued requisition ID, with the same
                   normalized title (a requisition reused for a different title is flagged
                   for review instead)
  application_url  the same job-specific application URL

Possible duplicates are only flagged (posting_duplicate_flags), never merged:
  title_location_description
                   same employer, same normalized title, at least one shared location, and
                   the same description after normalization (markup, whitespace, and
                   typography ignored). Conflicting evidence, such as two different
                   requisition IDs, is recorded on the flag for the reviewer. Missing
                   locations or descriptions never count as a match. Publication and
                   discovery dates are recorded as evidence only; discovery date is not
                   publication date.

Confidence is a category (strong, reviewed, possible), never a probability. Every rule
writes its name, version, and evidence. Groups never change availability: closing one record
does not close the others. A person can confirm or dismiss a flag and dissolve a group; a
dismissed flag or dissolved group is remembered, so the rules do not recreate it.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
from psycopg.types.json import Jsonb

from jobmarket import normalize
from jobmarket.store import version_comparison

RULE_VERSION = "2026-10-27.1"

_TITLE_SUFFIX = re.compile(r"\s+-\s+[^-]+,\s*[^-]+$")  # "Engineer - Dallas, TX, USA"
_TRACKING = re.compile(r"^(utm_.*|gh_src|source|src|ref|lever-source.*|lever-origin)$", re.I)
_ID_LIKE = re.compile(r"\d{4,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def normalize_title(title: str) -> str:
    return " ".join(_TITLE_SUFFIX.sub("", title).lower().split())


def normalize_requisition(value: str | None) -> str | None:
    cleaned = " ".join((value or "").split()).upper()
    return cleaned or None


def normalize_url(url: str | None) -> str | None:
    """Scheme and host lowercased, trailing slash and tracking parameters removed."""
    if not url:
        return None
    parts = urlsplit(url.strip())
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if not _TRACKING.match(k)))
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, "")
    )


def is_job_specific(url: str, source_posting_id: str) -> bool:
    """A URL for one job, not a board or careers front page: it names the posting's id or
    carries an id-like path or query value."""
    parts = urlsplit(url)
    return source_posting_id in url or bool(_ID_LIKE.search(parts.path + "?" + parts.query))


def location_set(location_text: str | None, offices: list | None) -> set[str]:
    found = set()
    for value in [location_text] + [
        (o.get("location") or o.get("name")) for o in (offices or []) if isinstance(o, dict)
    ]:
        if isinstance(value, str) and value.strip():
            text = " ".join(value.lower().split())
            if not text.startswith("workplace type:"):
                found.add(text)
    return found


@dataclass
class Record:
    posting_id: int
    employer_id: int
    employer: str
    board: str
    source_system: str
    source_posting_id: str
    title: str
    norm_title: str
    locations: set[str]
    requisition: str | None
    url: str | None
    description: str | None
    published_at: datetime | None
    discovered_at: datetime

    def summary(self) -> dict:
        return {
            "posting_id": self.posting_id,
            "board": f"{self.source_system}:{self.board}",
            "source_posting_id": self.source_posting_id,
            "title": self.title,
            "locations": sorted(self.locations),
            "requisition_id": self.requisition,
            "url": self.url,
            "source_published_at": self.published_at.isoformat() if self.published_at else None,
            "first_discovered_at": self.discovered_at.isoformat(),
        }


def load_records(conn: psycopg.Connection, now: datetime) -> list[Record]:
    """Every posting with content history (the ones duplicates matter for)."""
    rows = conn.execute(
        """
        SELECT p.posting_id, b.employer_id, e.name, b.board_key, b.source_system,
               p.source_posting_id, v.version_id, v.title, v.location_text, v.offices,
               v.requisition_id, coalesce(v.apply_url, v.absolute_url),
               co.source_published_at, p.first_discovered_at
        FROM source_postings p
        JOIN source_boards b ON b.board_id = p.board_id
        JOIN employers e ON e.employer_id = b.employer_id
        JOIN posting_versions v ON v.version_id = p.current_version_id
        JOIN posting_observations co ON co.observation_id = p.current_observation_id
        ORDER BY p.posting_id
        """
    ).fetchall()
    records = []
    for (pid, emp_id, emp, board, src, sid, vid, title, loc, offices, req, url, pub,
         first) in rows:  # fmt: skip
        comparison = version_comparison(conn, vid, normalize.CURRENT_NORMALIZATION, now)
        description = comparison.normalized.get("description") or None
        norm_url = normalize_url(url)
        records.append(
            Record(
                pid,
                emp_id,
                emp,
                board,
                src,
                sid,
                title,
                normalize_title(title),
                location_set(loc, offices),
                normalize_requisition(req),
                norm_url if norm_url and is_job_specific(norm_url, sid) else None,
                description,
                pub,
                first,
            )  # fmt: skip
        )
    return records


@dataclass
class RunResult:
    groups_created: int = 0
    members_added: int = 0
    flags_added: int = 0
    skipped: list[str] = field(default_factory=list)


def run_rules(conn: psycopg.Connection, now: datetime) -> RunResult:
    """Apply every rule once, in one transaction. Safe to repeat: existing groups and flags
    are kept, dissolved groups and dismissed flags are not recreated."""
    result = RunResult()
    with conn.transaction():
        records = load_records(conn, now)
        grouped = _active_memberships(conn)
        conflicts: list[tuple[list[Record], str]] = []

        by_req: dict[tuple[int, str], list[Record]] = defaultdict(list)
        for r in records:
            if r.requisition:
                by_req[(r.employer_id, r.requisition)].append(r)
        for (employer_id, req), members in sorted(by_req.items()):
            if len(members) < 2:
                continue
            if len({m.norm_title for m in members}) > 1:
                conflicts.append((members, f"requisition {req} is used for different titles"))
                continue
            _group(conn, "requisition_id", f"{employer_id}:{req}", members, {
                "employer_id": employer_id, "requisition_id": req,
            }, grouped, now, result)  # fmt: skip

        by_url: dict[str, list[Record]] = defaultdict(list)
        for r in records:
            if r.url:
                by_url[r.url].append(r)
        for url, members in sorted(by_url.items()):
            if len(members) >= 2:
                _group(conn, "application_url", url, members, {"url": url}, grouped, now, result)

        for members, why in conflicts:
            for a, b in combinations(members, 2):
                result.flags_added += _flag(
                    conn, a, b, "requisition_id_conflict", {"conflict": why}, now
                )

        buckets: dict[tuple, list[Record]] = defaultdict(list)
        for r in records:
            if r.description and r.locations:
                buckets[(r.employer_id, r.norm_title, r.description)].append(r)
        for members in buckets.values():
            for a, b in combinations(members, 2):
                shared = a.locations & b.locations
                if not shared:
                    continue
                if grouped.get(a.posting_id) and grouped.get(a.posting_id) == grouped.get(
                    b.posting_id
                ):
                    continue
                notes = []
                if a.requisition and b.requisition and a.requisition != b.requisition:
                    notes.append(
                        f"requisition IDs differ ({a.requisition} vs {b.requisition}): the "
                        "employer may treat these as separate openings"
                    )
                result.flags_added += _flag(conn, a, b, "title_location_description", {
                    "conflicts": notes,
                    "shared_locations": sorted(shared),
                    "matched": ["employer", "normalized title", "location",
                                "normalized description"],
                    "dates_note": "publication dates are evidence only; missing dates are "
                                  "neither a match nor a mismatch",
                }, now)  # fmt: skip
    return result


def _active_memberships(conn) -> dict[int, int]:
    return dict(
        conn.execute(
            "SELECT m.posting_id, m.group_id FROM duplicate_group_members m "
            "JOIN duplicate_groups g USING (group_id) WHERE g.status = 'active'"
        ).fetchall()
    )


def _group(conn, rule, key, members, evidence, grouped, now, result) -> None:
    existing = conn.execute(
        "SELECT group_id, status FROM duplicate_groups WHERE rule = %s AND match_key = %s",
        (rule, key),
    ).fetchone()
    if existing and existing[1] == "dissolved":
        result.skipped.append(f"{rule} {key}: dissolved after review; not recreated")
        return
    if existing:
        group_id = existing[0]
    else:
        free = [m for m in members if m.posting_id not in grouped]
        if len(free) < 2:
            result.skipped.append(f"{rule} {key}: members already in other groups")
            return
        group_id = conn.execute(
            "INSERT INTO duplicate_groups (rule, rule_version, match_key, confidence, evidence, "
            "created_at) VALUES (%s, %s, %s, 'strong', %s, %s) RETURNING group_id",
            (
                rule,
                RULE_VERSION,
                key,
                Jsonb({**evidence, "records": [m.summary() for m in members]}),
                now,
            ),
        ).fetchone()[0]
        result.groups_created += 1
    for m in members:
        if grouped.get(m.posting_id) not in (None, group_id):
            result.skipped.append(
                f"{rule} {key}: posting {m.posting_id} is already in group {grouped[m.posting_id]}"
            )
            continue
        added = conn.execute(
            "INSERT INTO duplicate_group_members (group_id, posting_id, added_at) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (group_id, m.posting_id, now),
        ).rowcount
        if added:
            grouped[m.posting_id] = group_id
            result.members_added += 1


def _flag(conn, a: Record, b: Record, rule: str, evidence: dict, now: datetime) -> int:
    low, high = sorted((a, b), key=lambda r: r.posting_id)
    return conn.execute(
        """
        INSERT INTO posting_duplicate_flags (posting_id, related_posting_id, reason, detected_at,
            rule, rule_version, evidence, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'open')
        ON CONFLICT (posting_id, related_posting_id) DO UPDATE SET
            reason = EXCLUDED.reason, detected_at = EXCLUDED.detected_at, rule = EXCLUDED.rule,
            rule_version = EXCLUDED.rule_version, evidence = EXCLUDED.evidence,
            status = 'open', reviewed_at = NULL, review_reason = NULL
        WHERE posting_duplicate_flags.status = 'superseded'
        """,
        (
            low.posting_id,
            high.posting_id,
            rule.replace("_", " "),
            now,
            rule,
            RULE_VERSION,
            Jsonb({**evidence, "records": [low.summary(), high.summary()]}),
        ),
    ).rowcount


# --- Review ---------------------------------------------------------------------------


class ReviewError(ValueError):
    """A review command was given something it cannot act on."""


def dismiss_flag(conn: psycopg.Connection, flag_id: int, reason: str, now: datetime) -> None:
    _review_flag(conn, flag_id, "dismissed", reason, now)


def confirm_flag(conn: psycopg.Connection, flag_id: int, reason: str, now: datetime) -> int:
    """Confirm a possible duplicate: group the two records (reviewed). Returns the group."""
    with conn.transaction():
        a, b = _review_flag(conn, flag_id, "confirmed", reason, now)
        grouped = _active_memberships(conn)
        if grouped.get(a) is not None and grouped.get(a) == grouped.get(b):
            return grouped[a]
        if grouped.get(a) is not None or grouped.get(b) is not None:
            raise ReviewError("one of the records is already in another group; dissolve it first")
        group_id = conn.execute(
            "INSERT INTO duplicate_groups (rule, rule_version, match_key, confidence, evidence, "
            "created_at) VALUES ('manual_review', %s, %s, 'reviewed', %s, %s) "
            "ON CONFLICT (rule, match_key) DO NOTHING RETURNING group_id",
            (RULE_VERSION, f"flag:{flag_id}", Jsonb({"flag_id": flag_id, "reason": reason}), now),
        ).fetchone()
        if group_id is None:
            raise ReviewError(f"flag {flag_id} was already confirmed once and then dissolved")
        for posting_id in (a, b):
            conn.execute(
                "INSERT INTO duplicate_group_members (group_id, posting_id, added_at) "
                "VALUES (%s, %s, %s)",
                (group_id[0], posting_id, now),
            )
        return group_id[0]


def _review_flag(conn, flag_id: int, status: str, reason: str, now: datetime):
    if not reason.strip():
        raise ReviewError("a review needs a reason")
    row = conn.execute(
        "UPDATE posting_duplicate_flags SET status = %s, reviewed_at = %s, review_reason = %s "
        "WHERE flag_id = %s AND status = 'open' RETURNING posting_id, related_posting_id",
        (status, now, reason, flag_id),
    ).fetchone()
    if row is None:
        raise ReviewError(f"there is no open flag {flag_id}")
    return row


def dissolve_group(conn: psycopg.Connection, group_id: int, reason: str, now: datetime) -> None:
    """Undo a group. Its members stay as separate records, and the same rule will not group
    them again."""
    if not reason.strip():
        raise ReviewError("dissolving a group needs a reason")
    updated = conn.execute(
        "UPDATE duplicate_groups SET status = 'dissolved', dissolved_at = %s, "
        "dissolved_reason = %s WHERE group_id = %s AND status = 'active'",
        (now, reason, group_id),
    ).rowcount
    if not updated:
        raise ReviewError(f"there is no active group {group_id}")
