"""The owner's private workspace: profile, saved jobs, application tracking, and visits.

Everything here reads and writes the private schema only (``schema``) and is scoped to the
signed-in account (``account_id``), so a guessed ID from another account, or one that does
not exist, finds nothing.

- The profile is exactly what the owner typed and confirmed. Nothing is inferred.
- Saved jobs and applications point at source postings. A confident duplicate group counts as
  one job in ordinary use: saving or tracking a posting whose group already has a saved or
  tracked posting reuses that entry instead of creating a second one. Groups that merge or
  split later change no stored row; two entries that end up in one group stay two entries.
- Application status is entered by the owner: an Apply link or a closed posting changes
  nothing. Any status may follow any other; a correction is a new event naming the one it
  corrects, so history is never overwritten.
- A visit starts at each sign-in, or after more than an hour idle (decision 2026-10-29). Its
  cutoff (the previous visit's last activity) stays fixed for the whole visit; the first
  visit has none.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql

from jobmarket.extraction_schema import EDUCATION_LEVEL
from jobmarket.listings import safe_link
from jobmarket.search import (
    ARRANGEMENT_CHOICES,
    EMPLOYMENT_CHOICES,
    PLACE_CHOICES,
    ROLE_CHOICES,
)
from jobmarket.skills import canonical_skill

ZONE = ZoneInfo("America/Chicago")
VISIT_IDLE = timedelta(hours=1)  # decision 2026-10-29

EXPERIENCE_KINDS = {"employment": "Employment", "internship": "Internship", "project": "Project"}
EDUCATION_LEVELS = {
    "high_school": "High school", "associate": "Associate degree", "bachelor": "Bachelor's degree",
    "master": "Master's degree", "doctorate": "Doctorate", "certificate": "Certificate",
    "bootcamp": "Bootcamp", "other": "Other",
}  # fmt: skip
assert set(EDUCATION_LEVELS) == set(EDUCATION_LEVEL)
EDUCATION_STATUS = {"completed": "Completed", "expected": "Expected"}
STATUSES = {
    "not_applied": "Not applied",
    "applied": "Applied",
    "interviewing": "Interviewing",
    "offer": "Offer",
    "rejected": "Rejected",
    "withdrawn": "Withdrawn",
}
NOTE_LIMIT = 5000


def _t(schema: str, table: str) -> sql.Composed:
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))


def _rows(conn, query, params=()) -> list[dict[str, Any]]:
    cur = conn.execute(query, params)
    names = [c.name for c in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def today(now: datetime) -> date:
    return now.astimezone(ZONE).date()


# ----------------------------------------------------------------------------- profile


@dataclass
class Skill:
    name: str
    note: str = ""
    link: str = ""


@dataclass
class Experience:
    kind: str
    title: str
    organization: str = ""
    start: date | None = None
    end: date | None = None
    current: bool = False
    note: str = ""
    link: str = ""


@dataclass
class Education:
    level: str
    field: str = ""
    institution: str = ""
    status: str = "completed"
    graduation: date | None = None


@dataclass
class Profile:
    skills: list[Skill] = field(default_factory=list)
    experiences: list[Experience] = field(default_factory=list)
    education: list[Education] = field(default_factory=list)
    preferred_roles: list[str] = field(default_factory=list)
    preferred_places: list[str] = field(default_factory=list)
    preferred_arrangements: list[str] = field(default_factory=list)
    preferred_employment: list[str] = field(default_factory=list)
    highlight_sponsorship: bool = False
    highlight_restrictions: bool = False
    version: int = 0  # 0: never saved
    confirmed_at: datetime | None = None


def _month(text: str) -> date | None:
    """'2025-05' (what <input type=month> sends) as the first of that month, or None."""
    text = (text or "").strip()
    if len(text) != 7 or text[4] != "-" or not (text[:4] + text[5:]).isdigit():
        raise ValueError("use the form YYYY-MM")
    year, month = int(text[:4]), int(text[5:])
    if not 1 <= month <= 12:
        raise ValueError("the month must be 01 to 12")
    if not 1950 <= year <= 2100:
        raise ValueError("the year looks wrong")
    return date(year, month, 1)


def month_text(value: date | None) -> str:
    return f"{value.year:04d}-{value.month:02d}" if value else ""


def _getlist(form: Mapping[str, Any], key: str) -> list[str]:
    getter = getattr(form, "getlist", None)
    if getter is not None:
        return [str(v) for v in getter(key)]
    value = form.get(key)
    if isinstance(value, list | tuple):
        return [str(v) for v in value]
    return [str(value)] if value else []


def _clean(text: str, limit: int) -> str:
    return " ".join((text or "").split())[:limit] if limit <= 120 else (text or "").strip()


@dataclass
class ProfileForm:
    """What the owner typed (strings, for redisplay) and the problems found in it."""

    profile: Profile
    raw: dict[str, list[dict[str, str]]]
    errors: dict[str, str]
    confirmed: bool

    @property
    def ok(self) -> bool:
        return not self.errors


def parse_profile_form(form: Mapping[str, Any], now: datetime) -> ProfileForm:
    """Validate the profile form. Blank rows are ignored; every problem is reported by field
    name ("skills-2-link") so the form can show it next to the value."""
    errors: dict[str, str] = {}
    this_month = today(now).replace(day=1)
    raw: dict[str, list[dict[str, str]]] = {"skills": [], "experiences": [], "education": []}
    profile = Profile()

    def rows(prefix: str, fields: tuple[str, ...]) -> list[dict[str, str]]:
        count = int(form.get(f"{prefix}-count", "0") or 0) if str(
            form.get(f"{prefix}-count", "0")).isdigit() else 0  # fmt: skip
        found = []
        for i in range(min(count, 100)):
            found.append({f: str(form.get(f"{prefix}-{i}-{f}", "") or "") for f in fields})
        return found

    seen_skills: set[str] = set()
    for i, row in enumerate(rows("skills", ("name", "note", "link"))):
        raw["skills"].append(row)
        name = _clean(row["name"], 80)
        if not name:
            if row["note"].strip() or row["link"].strip():
                errors[f"skills-{i}-name"] = "Add the skill's name, or clear this row."
            continue
        canonical, _ = canonical_skill(name)
        if canonical.lower() in seen_skills:
            errors[f"skills-{i}-name"] = f"{canonical} is already listed."
            continue
        seen_skills.add(canonical.lower())
        link = row["link"].strip()
        if link and safe_link(link) is None:
            errors[f"skills-{i}-link"] = "Use a full https:// link."
        if len(row["note"]) > 500:
            errors[f"skills-{i}-note"] = "Keep the note under 500 characters."
        profile.skills.append(Skill(name, row["note"].strip(), link))

    exp_fields = ("kind", "title", "organization", "start", "end", "current", "note", "link")
    for i, row in enumerate(rows("experiences", exp_fields)):
        raw["experiences"].append(row)
        title = _clean(row["title"], 120)
        if not title and not any(row[f].strip() for f in ("organization", "start", "end",
                                                           "note", "link")):  # fmt: skip
            continue
        key = f"experiences-{i}"
        if row["kind"] not in EXPERIENCE_KINDS:
            errors[f"{key}-kind"] = "Choose employment, internship, or project."
        if not title:
            errors[f"{key}-title"] = "Add a title."
        start = end = None
        try:
            start = _month(row["start"])
            if start > this_month:
                errors[f"{key}-start"] = "The start cannot be in the future."
        except ValueError as exc:
            errors[f"{key}-start"] = f"Start month: {exc}."
        current = row["current"] in ("on", "true", "1")
        if row["end"].strip():
            try:
                end = _month(row["end"])
                if end > this_month:
                    errors[f"{key}-end"] = "The end cannot be in the future (tick 'current')."
                elif start and end < start:
                    errors[f"{key}-end"] = "The end is before the start."
            except ValueError as exc:
                errors[f"{key}-end"] = f"End month: {exc}."
            if current:
                errors[f"{key}-current"] = "Either an end month or 'current', not both."
        elif not current:
            errors[f"{key}-end"] = "Add the end month, or tick 'current'."
        link = row["link"].strip()
        if link and safe_link(link) is None:
            errors[f"{key}-link"] = "Use a full https:// link."
        if len(row["note"]) > 1000:
            errors[f"{key}-note"] = "Keep the note under 1,000 characters."
        profile.experiences.append(Experience(
            row["kind"], title, _clean(row["organization"], 120), start, end, current,
            row["note"].strip(), link))  # fmt: skip

    edu_fields = ("level", "field", "institution", "status", "graduation")
    for i, row in enumerate(rows("education", edu_fields)):
        raw["education"].append(row)
        if not any(row[f].strip() for f in ("field", "institution", "graduation")) and not row[
                "level"]:  # fmt: skip
            continue
        key = f"education-{i}"
        if row["level"] not in EDUCATION_LEVELS:
            errors[f"{key}-level"] = "Choose the level."
        if row["status"] not in EDUCATION_STATUS:
            errors[f"{key}-status"] = "Choose completed or expected."
        graduation = None
        try:
            graduation = _month(row["graduation"]) if row["graduation"].strip() else None
        except ValueError as exc:
            errors[f"{key}-graduation"] = f"Graduation month: {exc}."
        if graduation is None and row["status"] == "expected":
            errors[f"{key}-graduation"] = "Add the expected graduation month."
        if graduation and row["status"] == "completed" and graduation > this_month:
            errors[f"{key}-graduation"] = "A completed degree cannot end in the future."
        if graduation and row["status"] == "expected" and graduation < this_month:
            errors[f"{key}-graduation"] = "An expected graduation should be this month or later."
        profile.education.append(Education(
            row["level"], _clean(row["field"], 120), _clean(row["institution"], 120),
            row["status"], graduation))  # fmt: skip

    preferences = (
        ("role", ROLE_CHOICES, "preferred_roles"),
        ("place", PLACE_CHOICES, "preferred_places"),
        ("arrangement", ARRANGEMENT_CHOICES, "preferred_arrangements"),
        ("employment", EMPLOYMENT_CHOICES, "preferred_employment"),
    )
    for name, choices, attr in preferences:
        values = _getlist(form, name)
        if any(v not in choices for v in values):
            errors[name] = "Unknown choice."
        setattr(profile, attr, [v for v in choices if v in values])
    profile.highlight_sponsorship = form.get("highlight_sponsorship") == "on"
    profile.highlight_restrictions = form.get("highlight_restrictions") == "on"
    confirmed = form.get("confirm") == "on"
    if not confirmed:
        errors["confirm"] = "Confirm that this describes your actual skills and experience."
    return ProfileForm(profile, raw, errors, confirmed)


def raw_rows(profile: Profile) -> dict[str, list[dict[str, str]]]:
    """A saved profile as form rows."""
    return {
        "skills": [{"name": s.name, "note": s.note, "link": s.link} for s in profile.skills],
        "experiences": [
            {"kind": e.kind, "title": e.title, "organization": e.organization,
             "start": month_text(e.start), "end": month_text(e.end),
             "current": "on" if e.current else "", "note": e.note, "link": e.link}
            for e in profile.experiences
        ],
        "education": [
            {"level": e.level, "field": e.field, "institution": e.institution,
             "status": e.status, "graduation": month_text(e.graduation)}
            for e in profile.education
        ],
    }  # fmt: skip


def load_profile(conn: psycopg.Connection, schema: str, account_id: int) -> Profile:
    row = conn.execute(
        sql.SQL(
            "SELECT version, confirmed_at, preferred_roles, preferred_places, "
            "preferred_arrangements, preferred_employment, highlight_sponsorship, "
            "highlight_restrictions FROM {} WHERE account_id = %s"
        ).format(_t(schema, "profile")),
        (account_id,),
    ).fetchone()
    if row is None:
        return Profile()
    profile = Profile(version=row[0], confirmed_at=row[1], preferred_roles=list(row[2]),
                      preferred_places=list(row[3]), preferred_arrangements=list(row[4]),
                      preferred_employment=list(row[5]), highlight_sponsorship=row[6],
                      highlight_restrictions=row[7])  # fmt: skip
    for name, note, link in conn.execute(sql.SQL(
            "SELECT name, note, link FROM {} WHERE account_id = %s ORDER BY position").format(
            _t(schema, "profile_skills")), (account_id,)):  # fmt: skip
        profile.skills.append(Skill(name, note or "", link or ""))
    for r in conn.execute(sql.SQL(
            "SELECT kind, title, organization, start_month, end_month, current, note, link "
            "FROM {} WHERE account_id = %s ORDER BY position").format(
            _t(schema, "profile_experiences")), (account_id,)):  # fmt: skip
        profile.experiences.append(Experience(r[0], r[1], r[2] or "", r[3], r[4], r[5],
                                              r[6] or "", r[7] or ""))  # fmt: skip
    for r in conn.execute(sql.SQL(
            "SELECT level, field, institution, status, graduation_month FROM {} "
            "WHERE account_id = %s ORDER BY position").format(
            _t(schema, "profile_education")), (account_id,)):  # fmt: skip
        profile.education.append(Education(r[0], r[1] or "", r[2] or "", r[3], r[4]))
    return profile


def save_profile(
    conn: psycopg.Connection, schema: str, account_id: int, profile: Profile, now: datetime
) -> int:
    """Replace the profile with this confirmed one, in one transaction. Returns the version."""
    with conn.transaction():
        version = conn.execute(
            sql.SQL(
                "INSERT INTO {t} (account_id, version, confirmed_at, updated_at, preferred_roles, "
                "preferred_places, preferred_arrangements, preferred_employment, "
                "highlight_sponsorship, highlight_restrictions) "
                "VALUES (%s, 1, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (account_id) DO UPDATE SET version = {t}.version + 1, "
                "confirmed_at = excluded.confirmed_at, updated_at = excluded.updated_at, "
                "preferred_roles = excluded.preferred_roles, "
                "preferred_places = excluded.preferred_places, "
                "preferred_arrangements = excluded.preferred_arrangements, "
                "preferred_employment = excluded.preferred_employment, "
                "highlight_sponsorship = excluded.highlight_sponsorship, "
                "highlight_restrictions = excluded.highlight_restrictions RETURNING version"
            ).format(t=_t(schema, "profile")),
            (account_id, now, now, profile.preferred_roles, profile.preferred_places,
             profile.preferred_arrangements, profile.preferred_employment,
             profile.highlight_sponsorship, profile.highlight_restrictions),
        ).fetchone()[0]  # fmt: skip
        for table in ("profile_skills", "profile_experiences", "profile_education"):
            conn.execute(sql.SQL("DELETE FROM {} WHERE account_id = %s").format(
                _t(schema, table)), (account_id,))  # fmt: skip
        for i, s in enumerate(profile.skills):
            conn.execute(sql.SQL(
                "INSERT INTO {} (account_id, position, name, canonical, note, link) "
                "VALUES (%s, %s, %s, %s, %s, %s)").format(_t(schema, "profile_skills")),
                (account_id, i, s.name, canonical_skill(s.name)[0].lower(), s.note or None,
                 s.link or None))  # fmt: skip
        for i, e in enumerate(profile.experiences):
            conn.execute(sql.SQL(
                "INSERT INTO {} (account_id, position, kind, title, organization, start_month, "
                "end_month, current, note, link) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            ).format(_t(schema, "profile_experiences")),
                (account_id, i, e.kind, e.title, e.organization or None, e.start, e.end,
                 e.current, e.note or None, e.link or None))  # fmt: skip
        for i, e in enumerate(profile.education):
            conn.execute(sql.SQL(
                "INSERT INTO {} (account_id, position, level, field, institution, status, "
                "graduation_month) VALUES (%s, %s, %s, %s, %s, %s, %s)").format(
                _t(schema, "profile_education")),
                (account_id, i, e.level, e.field or None, e.institution or None, e.status,
                 e.graduation))  # fmt: skip
    return version


# -------------------------------------------------------------------------- saved jobs


@dataclass(frozen=True)
class Saved:
    saved_id: int
    posting_id: int
    saved_at: datetime
    note: str


def saved_among(
    conn: psycopg.Connection, schema: str, account_id: int, posting_ids: Iterable[int]
) -> list[Saved]:
    """Currently saved entries for any of these postings (a job's group members)."""
    rows = conn.execute(
        sql.SQL(
            "SELECT saved_id, posting_id, saved_at, note FROM {} WHERE account_id = %s "
            "AND posting_id = ANY(%s) AND removed_at IS NULL ORDER BY saved_at, saved_id"
        ).format(_t(schema, "saved_jobs")),
        (account_id, list(posting_ids)),
    ).fetchall()
    return [Saved(r[0], r[1], r[2], r[3] or "") for r in rows]


def save_job(
    conn: psycopg.Connection,
    schema: str,
    account_id: int,
    posting_id: int,
    group_members: Iterable[int],
    now: datetime,
) -> Saved:
    """Save a posting. Repeating it, or saving another posting of the same confident group,
    returns the existing entry."""
    members = set(group_members) | {posting_id}
    existing = saved_among(conn, schema, account_id, members)
    if existing:
        return existing[0]
    row = conn.execute(
        sql.SQL(
            "INSERT INTO {t} (account_id, posting_id, saved_at) VALUES (%s, %s, %s) "
            "ON CONFLICT (account_id, posting_id) DO UPDATE SET removed_at = NULL, "
            "saved_at = CASE WHEN {t}.removed_at IS NULL THEN {t}.saved_at ELSE excluded.saved_at "
            "END RETURNING saved_id, posting_id, saved_at, note"
        ).format(t=_t(schema, "saved_jobs")),
        (account_id, posting_id, now),
    ).fetchone()
    return Saved(row[0], row[1], row[2], row[3] or "")


def unsave_job(
    conn: psycopg.Connection,
    schema: str,
    account_id: int,
    group_members: Iterable[int],
    now: datetime,
) -> int:
    """Unsave the job (every saved posting of its group). Notes and applications stay."""
    return conn.execute(
        sql.SQL(
            "UPDATE {} SET removed_at = %s WHERE account_id = %s AND posting_id = ANY(%s) "
            "AND removed_at IS NULL"
        ).format(_t(schema, "saved_jobs")),
        (now, account_id, list(group_members)),
    ).rowcount


def set_saved_note(
    conn: psycopg.Connection, schema: str, account_id: int, saved_id: int, note: str
) -> bool:
    if len(note) > NOTE_LIMIT:
        raise ValueError(f"keep the note under {NOTE_LIMIT:,} characters")
    return bool(conn.execute(
        sql.SQL("UPDATE {} SET note = %s WHERE saved_id = %s AND account_id = %s").format(
            _t(schema, "saved_jobs")),
        (note.strip() or None, saved_id, account_id)).rowcount)  # fmt: skip


def list_saved(conn: psycopg.Connection, schema: str, account_id: int) -> list[Saved]:
    rows = conn.execute(
        sql.SQL(
            "SELECT saved_id, posting_id, saved_at, note FROM {} WHERE account_id = %s "
            "AND removed_at IS NULL ORDER BY saved_at DESC, saved_id DESC"
        ).format(_t(schema, "saved_jobs")),
        (account_id,),
    ).fetchall()
    return [Saved(r[0], r[1], r[2], r[3] or "") for r in rows]


# ------------------------------------------------------------------------ applications


@dataclass(frozen=True)
class Event:
    event_id: int
    status: str
    event_date: date
    note: str
    recorded_at: datetime
    corrects_event_id: int | None
    corrected_by: int | None  # the event that corrects this one, if any


@dataclass(frozen=True)
class Application:
    application_id: int
    posting_id: int
    created_at: datetime
    events: list[Event]

    @property
    def current(self) -> Event | None:
        """The latest event that has not been corrected (by date, then by entry order)."""
        live = [e for e in self.events if e.corrected_by is None]
        return max(live, key=lambda e: (e.event_date, e.recorded_at, e.event_id), default=None)


class TrackingError(ValueError):
    """A status entry was not valid; the message is safe to show."""


def _events(conn, schema, application_id) -> list[Event]:
    rows = conn.execute(
        sql.SQL(
            "SELECT e.event_id, e.status, e.event_date, e.note, e.recorded_at, "
            "e.corrects_event_id, c.event_id FROM {t} e LEFT JOIN {t} c "
            "ON c.corrects_event_id = e.event_id WHERE e.application_id = %s "
            "ORDER BY e.event_date, e.recorded_at, e.event_id"
        ).format(t=_t(schema, "application_events")),
        (application_id,),
    ).fetchall()
    return [Event(r[0], r[1], r[2], r[3] or "", r[4], r[5], r[6]) for r in rows]


def application_among(
    conn: psycopg.Connection, schema: str, account_id: int, posting_ids: Iterable[int]
) -> Application | None:
    """The tracked application for any of these postings (a job's group members)."""
    row = conn.execute(
        sql.SQL(
            "SELECT application_id, posting_id, created_at FROM {} WHERE account_id = %s "
            "AND posting_id = ANY(%s) ORDER BY created_at, application_id LIMIT 1"
        ).format(_t(schema, "applications")),
        (account_id, list(posting_ids)),
    ).fetchone()
    if row is None:
        return None
    return Application(row[0], row[1], row[2], _events(conn, schema, row[0]))


def record_status(
    conn: psycopg.Connection,
    schema: str,
    account_id: int,
    posting_id: int,
    group_members: Iterable[int],
    status: str,
    event_date: date,
    note: str,
    now: datetime,
    corrects_event_id: int | None = None,
) -> Application:
    """Add a status event (or a correction of an earlier one) to the job's application,
    creating the application on first use."""
    if status not in STATUSES:
        raise TrackingError("choose one of the listed statuses")
    if event_date > today(now):
        raise TrackingError("the date cannot be in the future")
    if event_date < date(2000, 1, 1):
        raise TrackingError("the date looks wrong")
    if len(note) > NOTE_LIMIT:
        raise TrackingError(f"keep the note under {NOTE_LIMIT:,} characters")
    members = set(group_members) | {posting_id}
    with conn.transaction():
        application = application_among(conn, schema, account_id, members)
        if application is None:
            if corrects_event_id is not None:
                raise TrackingError("there is nothing to correct yet")
            conn.execute(
                sql.SQL(
                    "INSERT INTO {} (account_id, posting_id, created_at) VALUES (%s, %s, %s) "
                    "ON CONFLICT (account_id, posting_id) DO NOTHING"
                ).format(_t(schema, "applications")),
                (account_id, posting_id, now),
            )
            application = application_among(conn, schema, account_id, members)
        if corrects_event_id is not None:
            target = next((e for e in application.events if e.event_id == corrects_event_id),
                          None)  # fmt: skip
            if target is None:
                raise TrackingError("that entry does not belong to this job")
            if target.corrected_by is not None:
                raise TrackingError("that entry was already corrected; correct the newer one")
        conn.execute(
            sql.SQL(
                "INSERT INTO {} (application_id, status, event_date, note, recorded_at, "
                "corrects_event_id) VALUES (%s, %s, %s, %s, %s, %s)"
            ).format(_t(schema, "application_events")),
            (application.application_id, status, event_date, note.strip() or None, now,
             corrects_event_id),
        )  # fmt: skip
        return application_among(conn, schema, account_id, members)


def list_applications(conn: psycopg.Connection, schema: str, account_id: int) -> list[Application]:
    rows = conn.execute(
        sql.SQL(
            "SELECT application_id, posting_id, created_at FROM {} WHERE account_id = %s "
            "ORDER BY created_at DESC, application_id DESC"
        ).format(_t(schema, "applications")),
        (account_id,),
    ).fetchall()
    return [Application(r[0], r[1], r[2], _events(conn, schema, r[0])) for r in rows]


# ------------------------------------------------------------------------------ visits


@dataclass(frozen=True)
class Visit:
    visit_id: int
    started_at: datetime
    cutoff: datetime | None  # None: the first visit, a baseline


def touch_visit(
    conn: psycopg.Connection, schema: str, account_id: int, session_id: int, now: datetime
) -> Visit:
    """The current visit, starting a new one at a new sign-in or after an hour idle."""
    latest = conn.execute(
        sql.SQL(
            "SELECT visit_id, session_id, started_at, last_activity_at, cutoff FROM {} "
            "WHERE account_id = %s ORDER BY started_at DESC, visit_id DESC LIMIT 1"
        ).format(_t(schema, "visits")),
        (account_id,),
    ).fetchone()
    if latest is not None:
        visit_id, sid, started, last, cutoff = latest
        if sid == session_id and now - last <= VISIT_IDLE:
            if now > last:
                conn.execute(
                    sql.SQL("UPDATE {} SET last_activity_at = %s WHERE visit_id = %s").format(
                        _t(schema, "visits")
                    ),
                    (now, visit_id),
                )
            return Visit(visit_id, started, cutoff)
    cutoff = latest[3] if latest is not None else None
    row = conn.execute(
        sql.SQL(
            "INSERT INTO {} (account_id, session_id, started_at, last_activity_at, cutoff) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING visit_id"
        ).format(_t(schema, "visits")),
        (account_id, session_id, now, now, min(cutoff, now) if cutoff else None),
    ).fetchone()
    return Visit(row[0], now, cutoff)
