"""Job search over the analytics serving views (analytics_serving.srv_postings, srv_boards).

The serving views are live views over the ingestion tables, so search always reflects the
latest collection and extraction; nothing here calls a source, the model, or dbt.

Search state lives in the URL (SearchParams.query_string), so every result page can be
shared and restored. Every value is validated against the allowed choices before it is
used, and all values reach SQL as bound parameters.

Filter semantics
- Text search matches the title and description (stemmed full-text search) or the
  employer name.
- Within one filter, choices are combined with OR (any of them); different filters are
  combined with AND. Skills can be switched to "all of these".
- For each structured filter a listing is a match (the extracted value confirms it),
  unknown (not extracted yet, or the listing does not state it clearly), or a contradiction
  (the listing states something else). Unknown listings are shown only when
  include_unknown is on, and every one carries the reasons; contradictions never are.
- Confident duplicate groups count as one result (an "opportunity"). A group is shown when
  any of its listings matches. Its availability rolls up from its listings: observed if
  any is observed, possibly unavailable if any is, closed only if all are, otherwise
  assumed inactive. The listing shown first is the most recently observed one; the others
  stay reachable from the detail page.
- Active results are observed and possibly unavailable listings; assumed inactive and
  closed ones are in the archived view. Availability is the ingestion's state (Module 6);
  this module never changes or re-derives it.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import psycopg
from psycopg import sql

from jobmarket.availability import ACTIVE_STATES

PAGE_SIZE = 25  # decision 2026-10-28
STALE_AFTER = timedelta(hours=36)  # decision 2026-10-28: "may be out of date" note
ARCHIVED_STATES = ("assumed_inactive", "closed")
SERVING_SCHEMA = "analytics_serving"
MAX_SKILL_NAME = 80  # longer than any skill name; anything longer is not a skill

VIEWS = ("active", "archived")
POPULATIONS = ("kept", "all")
LEVELS = ("early", "internship", "any")
SORTS = ("discovered", "published")
SKILL_MODES = ("any", "all")
SPONSORSHIP = ("", "offers", "not_refused")

ROLE_CHOICES = {
    "data_analysis": "Data analysis",
    "bi": "Business intelligence",
    "data_engineering": "Data engineering",
    "analytics_engineering": "Analytics engineering",
    "backend": "Backend",
    "software_engineering": "Software engineering",
    "full_stack": "Full stack",
    "data_science": "Data science",
    "ml": "Machine learning",
    "other": "Other roles",
}
# Location choices over the extraction's derived place.
PLACE_CHOICES = {
    "dfw": ("Dallas-Fort Worth", ("local_dfw", "local_both")),
    "houston": ("Houston", ("local_houston", "local_both")),
    "remote_texas": ("Remote, open to Texas", ("remote_texas",)),
    "remote_unclear": ("Remote, unclear where", ("remote_ambiguous",)),
    "elsewhere": ("Elsewhere", ("elsewhere", "remote_elsewhere")),
}
ARRANGEMENT_CHOICES = {"onsite": "On site", "hybrid": "Hybrid", "remote": "Remote"}
# Bands of the smallest required overall minimum, by whole years (decision 2026-10-28):
# 0-1 covers minimums below 2 years, 2-4 below 5, 5+ the rest.
EXPERIENCE_CHOICES = {"0-1": ("0-1 years", 0, 2), "2-4": ("2-4 years", 2, 5),
                      "5+": ("5+ years", 5, None)}  # fmt: skip
EMPLOYMENT_CHOICES = {
    "full_time": "Full-time",
    "part_time": "Part-time",
    "contract": "Contract",
    "contract_to_hire": "Contract to hire",
    "temporary": "Temporary",
    "internship": "Internship",
    "apprenticeship": "Apprenticeship",
}
LEVEL_CHOICES = {"early": "Early career", "internship": "Internships", "any": "All levels"}
SPONSORSHIP_CHOICES = {
    "": "Any",
    "offers": "Offers visa sponsorship",
    "not_refused": "Does not refuse sponsorship",
}
SORT_CHOICES = {"discovered": "Newest discovered", "published": "Source date"}


class ServingUnavailable(Exception):
    """The serving views are missing (analytics has not been built)."""


@dataclass(frozen=True)
class SearchParams:
    q: str = ""
    view: str = "active"
    population: str = "kept"
    level: str = "early"
    roles: tuple[str, ...] = ()
    employers: tuple[int, ...] = ()
    places: tuple[str, ...] = ()
    arrangements: tuple[str, ...] = ()
    experience: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    skill_mode: str = "any"
    sponsorship: str = ""
    employment: tuple[str, ...] = ()
    include_unknown: bool = True
    sort: str = "discovered"
    page: int = 1
    new_only: bool = False  # signed in: only jobs discovered since the previous visit

    def query_pairs(self, **changes) -> list[tuple[str, str]]:
        """URL parameters for this state (defaults omitted), with optional changes."""
        p = replace(self, **changes)
        pairs: list[tuple[str, str]] = []
        if p.q:
            pairs.append(("q", p.q))
        for name, default in (("view", "active"), ("population", "kept"), ("level", "early"),
                              ("skill_mode", "any"), ("sponsorship", ""),
                              ("sort", "discovered")):  # fmt: skip
            value = getattr(p, name)
            if value != default:
                pairs.append((name, value))
        for name, key in (("roles", "role"), ("employers", "employer"), ("places", "place"),
                          ("arrangements", "arrangement"), ("experience", "experience"),
                          ("skills", "skill"), ("employment", "employment")):  # fmt: skip
            pairs.extend((key, str(v)) for v in getattr(p, name))
        if not p.include_unknown:
            pairs.append(("unknown", "exclude"))
        if p.new_only:
            pairs.append(("new", "1"))
        if p.page != 1:
            pairs.append(("page", str(p.page)))
        return pairs

    def query_string(self, **changes) -> str:
        return urlencode(self.query_pairs(**changes))

    @property
    def structured_filters(self) -> bool:
        return bool(
            self.level != "any" or self.roles or self.places or self.arrangements
            or self.experience or self.skills or self.sponsorship or self.employment
        )  # fmt: skip


@dataclass(frozen=True)
class Choices:
    """Values a search may use: the fixed choices plus employers and skills in the data."""

    employers: dict[int, str]
    skills: dict[str, str]  # lowercase -> canonical name


def _many(args: Mapping[str, Any], key: str) -> list[str]:
    getlist = getattr(args, "getlist", None)
    if getlist is not None:
        values = getlist(key)
    else:
        value = args.get(key)
        values = value if isinstance(value, list | tuple) else ([value] if value else [])
    return [str(v).strip() for v in values if str(v).strip()]


def _one(args, key: str, allowed: Iterable[str], default: str, problems: list[str]) -> str:
    values = _many(args, key)
    if not values:
        return default
    if values[0] in allowed:
        return values[0]
    problems.append(f"Ignored an unknown {key} value.")
    return default


def _unique(values: Iterable) -> tuple:
    return tuple(dict.fromkeys(values))


def parse_params(args: Mapping[str, Any], choices: Choices) -> tuple[SearchParams, list[str]]:
    """Search parameters from URL arguments. Unknown values are dropped and reported."""
    problems: list[str] = []

    def several(key: str, allowed: Mapping[str, Any]) -> tuple[str, ...]:
        values = _many(args, key)
        kept = [v for v in values if v in allowed]
        if len(kept) < len(values):
            problems.append(f"Ignored unknown {key} value(s).")
        return _unique(kept)

    employers = []
    for raw in _many(args, "employer"):
        if raw.isdigit() and int(raw) in choices.employers:
            employers.append(int(raw))
        else:
            problems.append("Ignored an unknown employer.")
    skills = []
    for raw in _many(args, "skill"):
        canonical = choices.skills.get(raw.lower())
        if canonical:
            skills.append(canonical)
        elif len(raw) <= MAX_SKILL_NAME and raw.isprintable():
            # Kept, so the filter matches nothing instead of silently showing everything.
            skills.append(raw)
            problems.append(f"No listing asks for {raw} as a required or preferred skill.")
        else:
            problems.append("Ignored an invalid skill name.")
    page_raw = (_many(args, "page") or ["1"])[0]
    page = int(page_raw) if page_raw.isdigit() and int(page_raw) >= 1 else 1
    if page_raw != str(page):
        problems.append("Ignored an invalid page number.")
    unknown = _one(args, "unknown", ("include", "exclude"), "include", problems)
    params = SearchParams(
        q=" ".join((_many(args, "q") or [""])[0].split()),
        view=_one(args, "view", VIEWS, "active", problems),
        population=_one(args, "population", POPULATIONS, "kept", problems),
        level=_one(args, "level", LEVELS, "early", problems),
        roles=several("role", ROLE_CHOICES),
        employers=_unique(employers),
        places=several("place", PLACE_CHOICES),
        arrangements=several("arrangement", ARRANGEMENT_CHOICES),
        experience=several("experience", EXPERIENCE_CHOICES),
        skills=_unique(skills),
        skill_mode=_one(args, "skill_mode", SKILL_MODES, "any", problems),
        sponsorship=_one(args, "sponsorship", SPONSORSHIP, "", problems),
        employment=several("employment", EMPLOYMENT_CHOICES),
        include_unknown=unknown == "include",
        sort=_one(args, "sort", SORTS, "discovered", problems),
        page=page,
        new_only=_one(args, "new", ("1",), "", problems) == "1",
    )
    return params, list(_unique(problems))


# Why a listing without a confirmed value is "unknown".
_PENDING = sql.SQL(
    "CASE WHEN s.extraction_status IN ('pending', 'running', 'not_queued', 'succeeded') "
    "THEN 'structured details pending' ELSE 'structured details unavailable' END"
)


@dataclass
class _Filter:
    label: str
    state: sql.Composable  # 2 match, 1 unknown, 0 contradiction
    reason: sql.Composable  # text, used when state = 1


def _values_reason(column: str, extra: str = "") -> sql.Composable:
    return sql.SQL(
        "CASE WHEN NOT s.extracted THEN {pending} "
        "WHEN s.{col} = 'not_stated' THEN 'not stated' "
        "WHEN s.{col} = 'ambiguous' THEN 'unclear in the listing' " + extra +
        "ELSE 'not determined' END"
    ).format(pending=_PENDING, col=sql.Identifier(column))  # fmt: skip


def _filters(p: SearchParams, values: dict[str, Any]) -> list[_Filter]:
    found: list[_Filter] = []
    if p.level in ("early", "internship"):
        wanted, others = (("yes",), ("no", "internship")) if p.level == "early" else (
            ("internship",), ("yes", "no"))  # fmt: skip
        values["level_match"], values["level_other"] = list(wanted), list(others)
        found.append(_Filter(
            LEVEL_CHOICES[p.level],
            sql.SQL("CASE WHEN NOT s.extracted THEN 1 "
                    "WHEN s.early_career = ANY(%(level_match)s) THEN 2 "
                    "WHEN s.early_career = ANY(%(level_other)s) THEN 0 ELSE 1 END"),
            sql.SQL("CASE WHEN NOT s.extracted THEN {} "
                    "ELSE 'early-career status unclear' END").format(_PENDING),
        ))  # fmt: skip
    if p.roles:
        values["roles"] = list(p.roles)
        found.append(_Filter(
            "Role",
            sql.SQL("CASE WHEN NOT s.extracted THEN 1 "
                    "WHEN s.role_categories && %(roles)s::text[] THEN 2 "
                    "WHEN 'uncertain' = ANY(s.role_categories) THEN 1 ELSE 0 END"),
            sql.SQL("CASE WHEN NOT s.extracted THEN {} ELSE 'role unclear' END").format(_PENDING),
        ))  # fmt: skip
    if p.places:
        values["places"] = [v for key in p.places for v in PLACE_CHOICES[key][1]]
        values["remote_texas"] = "remote_texas" in p.places
        found.append(_Filter(
            "Location",
            sql.SQL("CASE WHEN NOT s.extracted THEN 1 WHEN s.place = ANY(%(places)s) THEN 2 "
                    "WHEN s.place IS NULL OR s.place = 'unknown' THEN 1 "
                    "WHEN %(remote_texas)s AND s.place = 'remote_ambiguous' THEN 1 ELSE 0 END"),
            sql.SQL("CASE WHEN NOT s.extracted THEN {} WHEN s.place IS NULL OR "
                    "s.place = 'unknown' THEN 'location not determined' "
                    "ELSE 'remote, unclear where' END").format(_PENDING),
        ))  # fmt: skip
    if p.arrangements:
        values["arrangements"] = list(p.arrangements)
        found.append(_Filter(
            "Work arrangement",
            sql.SQL("CASE WHEN NOT s.extracted THEN 1 "
                    "WHEN s.work_arrangement = ANY(%(arrangements)s) THEN 2 "
                    "WHEN s.work_arrangement IS NULL OR s.work_arrangement IN "
                    "('not_stated', 'ambiguous', 'multiple') THEN 1 ELSE 0 END"),
            _values_reason(
                "work_arrangement",
                "WHEN s.work_arrangement = 'multiple' THEN 'several arrangements listed' ",
            ),
        ))  # fmt: skip
    if p.experience:
        bands = []
        for i, key in enumerate(p.experience):
            _, low, high = EXPERIENCE_CHOICES[key]
            values[f"exp_low_{i}"] = low
            if high is None:
                bands.append(sql.SQL(f"s.overall_min_years >= %(exp_low_{i})s"))
            else:
                values[f"exp_high_{i}"] = high
                bands.append(sql.SQL(
                    f"(s.overall_min_years >= %(exp_low_{i})s AND "
                    f"s.overall_min_years < %(exp_high_{i})s)"))  # fmt: skip
        found.append(_Filter(
            "Experience",
            sql.SQL("CASE WHEN NOT s.extracted THEN 1 WHEN s.overall_min_years IS NULL THEN 1 "
                    "WHEN {} THEN 2 ELSE 0 END").format(sql.SQL(" OR ").join(bands)),
            sql.SQL("CASE WHEN NOT s.extracted THEN {} "
                    "ELSE 'no required minimum stated' END").format(_PENDING),
        ))  # fmt: skip
    if p.skills:
        values["skills"] = list(p.skills)
        test = "&&" if p.skill_mode == "any" else "@>"
        found.append(_Filter(
            "Skills",
            sql.SQL("CASE WHEN NOT s.extracted THEN 1 "
                    f"WHEN s.requested_skills {test} %(skills)s::text[] THEN 2 ELSE 0 END"),
            _PENDING,
        ))  # fmt: skip
    if p.sponsorship == "offers":
        found.append(_Filter(
            "Sponsorship",
            sql.SQL("CASE WHEN NOT s.extracted THEN 1 WHEN s.sponsorship = 'supported' THEN 2 "
                    "WHEN s.sponsorship = 'not_supported' THEN 0 ELSE 1 END"),
            _values_reason("sponsorship"),
        ))  # fmt: skip
    elif p.sponsorship == "not_refused":
        found.append(_Filter(
            "Sponsorship",
            sql.SQL("CASE WHEN NOT s.extracted THEN 1 "
                    "WHEN s.sponsorship = 'not_supported' THEN 0 ELSE 2 END"),
            _PENDING,
        ))  # fmt: skip
    if p.employment:
        values["employment"] = list(p.employment)
        found.append(_Filter(
            "Employment type",
            sql.SQL("CASE WHEN NOT s.extracted THEN 1 "
                    "WHEN s.employment_type = ANY(%(employment)s) THEN 2 "
                    "WHEN s.employment_type IS NULL OR s.employment_type IN "
                    "('not_stated', 'ambiguous') THEN 1 ELSE 0 END"),
            _values_reason("employment_type"),
        ))  # fmt: skip
    return found


# Columns the search reads. Never s.*: that would compute every listing's search vector.
_COLUMNS = sql.SQL(", ").join(
    sql.SQL("s.{}").format(sql.Identifier(c))
    for c in (
        "posting_id", "employer_id", "employer_name", "source_system", "title",
        "location_text", "first_discovered_at", "last_observed_at", "content_seen_at",
        "version_no", "source_published_at", "source_created_at", "availability_state",
        "qualifying_absences", "retained", "cohort", "opportunity_key", "extracted",
        "extraction_status", "role_categories", "early_career", "place", "work_arrangement",
        "sponsorship", "employment_type", "overall_min_years", "requested_skills",
        "salary_range", "board_last_complete_at", "board_on_hold",
    )
)  # fmt: skip


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _grouped(p: SearchParams, schema: str, values: dict[str, Any]) -> sql.Composable:
    """One row per matching opportunity (any view), with the shown listing's columns, the
    group's rolled-up values, and the counts per view (total, other_total).

    Everything is computed with window functions in one pass over the population, so group
    members that fail the text or employer filter still count toward the group's
    availability, and no join depends on the planner's row estimates.
    """
    srv = sql.Identifier(schema)
    hard = [sql.SQL("true")]
    if p.employers:
        values["employers"] = list(p.employers)
        hard.append(sql.SQL("s.employer_id = ANY(%(employers)s)"))
    if p.q:
        values["q"], values["q_like"] = p.q, f"%{_escape_like(p.q)}%"
        # The subquery lets PostgreSQL use the full-text index; the employer name is a
        # plain case-insensitive match.
        hard.append(sql.SQL(
            "(s.posting_id IN (SELECT t.posting_id FROM {srv}.srv_postings t WHERE "
            "t.search_vector @@ websearch_to_tsquery('english', %(q)s)) "
            "OR s.employer_name ILIKE %(q_like)s)").format(srv=srv))  # fmt: skip
    hard_sql = sql.SQL(" AND ").join(hard)
    filters = _filters(p, values)
    if filters:
        states = sql.SQL(", ").join(
            sql.SQL("({}) AS f{}").format(f.state, sql.Literal(i)) for i, f in enumerate(filters)
        )
        fit = sql.SQL("LEAST({})").format(
            sql.SQL(", ").join(sql.SQL("f{}").format(sql.Literal(i)) for i in range(len(filters)))
        )
        reasons = sql.SQL("array_remove(ARRAY[{}]::text[], NULL)").format(sql.SQL(", ").join(
            sql.SQL("CASE WHEN f{i} = 1 THEN {label} || ': ' || ({reason}) END").format(
                i=sql.Literal(i), label=sql.Literal(f.label), reason=f.reason)
            for i, f in enumerate(filters)))  # fmt: skip
        # Filter states are computed once per listing in the inner query. A listing that
        # fails the text or employer filter gets fit -1: it cannot qualify its group.
        scored = sql.SQL(
            "SELECT s.*, CASE WHEN s.passes THEN {fit} ELSE -1 END AS fit, "
            "{reasons} AS unknown_reasons "
            "FROM (SELECT s.*, {hard} AS passes, {states} FROM pop s) s"
        ).format(fit=fit, reasons=reasons, hard=hard_sql, states=states)
    else:
        scored = sql.SQL(
            "SELECT s.*, CASE WHEN {hard} THEN 2 ELSE -1 END AS fit, "
            "ARRAY[]::text[] AS unknown_reasons FROM pop s"
        ).format(hard=hard_sql)
    values["min_fit"] = 1 if p.include_unknown else 2
    population = sql.SQL("WHERE s.retained") if p.population == "kept" else sql.SQL("")
    return sql.SQL(
        """
        WITH pop AS (SELECT {columns} FROM {srv}.srv_postings s {population})
        SELECT w.*,
               count(*) FILTER (WHERE w.group_state = ANY(%(view_states)s)) OVER () AS total,
               count(*) FILTER (WHERE w.group_state = ANY(%(other_states)s)) OVER ()
                   AS other_total
        FROM (
            SELECT s.*,
                   CASE WHEN bool_or(s.availability_state = 'observed') OVER g THEN 'observed'
                        WHEN bool_or(s.availability_state = 'possibly_unavailable') OVER g
                            THEN 'possibly_unavailable'
                        WHEN bool_and(s.availability_state = 'closed') OVER g THEN 'closed'
                        ELSE 'assumed_inactive' END AS group_state,
                   min(s.first_discovered_at) OVER g AS discovered_at,
                   max(s.last_observed_at) OVER g AS group_last_seen_at,
                   count(*) OVER g AS records,
                   min(s.posting_id) OVER g AS first_posting_id,
                   max(s.fit) OVER g AS group_fit,
                   array_agg(s.location_text) OVER g AS group_locations,
                   first_value(s.unknown_reasons) OVER best AS group_unknown_reasons,
                   first_value(s.cohort) OVER first_found AS group_cohort,
                   row_number() OVER shown AS shown_rank
            FROM ({scored}) s
            WINDOW g AS (PARTITION BY s.opportunity_key),
                   shown AS (PARTITION BY s.opportunity_key
                             ORDER BY s.last_observed_at DESC, s.posting_id),
                   best AS (PARTITION BY s.opportunity_key
                            ORDER BY s.fit DESC, cardinality(s.unknown_reasons), s.posting_id),
                   first_found AS (PARTITION BY s.opportunity_key
                                   ORDER BY s.first_discovered_at, s.posting_id)
        ) w
        WHERE w.shown_rank = 1 AND w.group_fit >= %(min_fit)s
          AND (NOT %(new_only)s OR w.discovered_at > %(new_cutoff)s)
        """
    ).format(srv=srv, population=population, scored=scored, columns=_COLUMNS)


_ORDER = {
    "discovered": "o.discovered_at DESC, o.first_posting_id DESC",
    "published": "coalesce(o.source_published_at, o.source_created_at) DESC NULLS LAST, "
    "o.discovered_at DESC, o.first_posting_id DESC",
}


@dataclass(frozen=True)
class ResultRow:
    opportunity_key: str
    posting_id: int  # the listing shown for the opportunity
    title: str
    employer: str
    source_system: str
    location_text: str | None
    locations: list[str]
    records: int
    group_state: str
    availability_state: str  # of the listing shown
    qualifying_absences: int
    discovered_at: datetime
    cohort: str
    last_seen_at: datetime
    board_last_complete_at: datetime | None
    board_on_hold: bool
    stale: bool
    source_date: datetime | None
    source_date_kind: str | None  # "published" (Greenhouse) or "created" (Lever)
    content_updated_at: datetime | None  # when the current text was first seen, if edited
    extraction_status: str
    early_career: str | None
    place: str | None
    work_arrangement: str | None
    employment_type: str | None
    sponsorship: str | None
    salary_range: dict | None
    fit: int
    unknown_reasons: list[str]
    skill_notes: list[str] = field(default_factory=list)
    is_new: bool = False  # discovered after the previous visit (signed-in owner only)


@dataclass(frozen=True)
class SearchResult:
    params: SearchParams
    rows: list[ResultRow]
    total: int  # opportunities matching, in this view
    other_view_total: int  # matching opportunities in the other view (active/archived)
    page: int
    pages: int
    page_size: int
    problems: list[str]

    @property
    def first(self) -> int:
        return 0 if not self.total else (self.page - 1) * self.page_size + 1

    @property
    def last(self) -> int:
        return min(self.total, self.page * self.page_size)


def _skill_notes(extraction: dict | None, skills: Iterable[str]) -> list[str]:
    """How the shown listing asks for each selected skill ("Python: one of Python or Java")."""
    if not extraction or not skills:
        return []
    notes = []
    for skill in skills:
        groups = [g for g in extraction.get("skill_groups", [])
                  if g.get("status") in ("required", "preferred")
                  and any(m.get("canonical") == skill for m in g.get("members", []))]  # fmt: skip
        alone = [
            s
            for s in extraction.get("skills", [])
            if s.get("canonical") == skill and s.get("status") in ("required", "preferred")
        ]
        if groups:
            g = groups[0]
            notes.append(f"{skill}: {g['status']}, one of {g['display_text']}")
        elif alone:
            notes.append(f"{skill}: {alone[0]['status']}")
    return notes


def _check_serving(conn: psycopg.Connection, schema: str) -> None:
    found = conn.execute(
        "SELECT to_regclass(%s), to_regclass(%s)",
        (f'"{schema}".srv_postings', f'"{schema}".srv_boards'),
    ).fetchone()
    if None in found:
        raise ServingUnavailable(
            "the job-search views are not built yet; run `jobmarket analytics build`"
        )


def search(
    conn: psycopg.Connection,
    params: SearchParams,
    now: datetime,
    problems: list[str] | None = None,
    schema: str = SERVING_SCHEMA,
    page_size: int = PAGE_SIZE,
    new_cutoff: datetime | None = None,
) -> SearchResult:
    """One page of matching opportunities, newest first (or by source date).

    new_cutoff (the previous visit's last activity, for a signed-in owner) marks
    opportunities first discovered after it; with params.new_only only those are returned.
    Without a cutoff (signed out, or a first visit) nothing is new.
    """
    _check_serving(conn, schema)
    if params.new_only and new_cutoff is None:
        problems = [*(problems or []), "'New since your last visit' needs a previous visit; "
                    "showing all jobs."]  # fmt: skip
        params = replace(params, new_only=False)
    values: dict[str, Any] = {"new_only": params.new_only, "new_cutoff": new_cutoff}
    grouped = _grouped(params, schema, values)
    view_states = list(ACTIVE_STATES if params.view == "active" else ARCHIVED_STATES)
    other_states = list(ARCHIVED_STATES if params.view == "active" else ACTIVE_STATES)
    values["view_states"], values["other_states"] = view_states, other_states
    values["stale_before"] = now - STALE_AFTER
    query = sql.SQL(
        """
        SELECT o.opportunity_key, o.posting_id, o.title, o.employer_name, o.source_system,
               o.location_text, o.group_locations, o.records, o.group_state,
               o.availability_state, o.qualifying_absences, o.discovered_at, o.group_cohort,
               o.group_last_seen_at, o.board_last_complete_at, o.board_on_hold,
               o.board_last_complete_at IS NULL
                   OR o.board_last_complete_at < %(stale_before)s AS stale,
               coalesce(o.source_published_at, o.source_created_at),
               CASE WHEN o.source_published_at IS NOT NULL THEN 'published'
                    WHEN o.source_created_at IS NOT NULL THEN 'created' END,
               CASE WHEN o.version_no > 1 THEN o.content_seen_at END,
               o.extraction_status, o.early_career, o.place, o.work_arrangement,
               o.employment_type, o.sponsorship, o.salary_range, o.group_fit,
               o.group_unknown_reasons, o.discovered_at > %(new_cutoff)s,
               o.total, o.other_total
        FROM ({grouped}) o
        WHERE o.group_state = ANY(%(view_states)s)
        ORDER BY {order}
        LIMIT %(limit)s OFFSET %(offset)s
        """
    ).format(grouped=grouped, order=sql.SQL(_ORDER[params.sort]))

    def fetch(page: int) -> list[tuple]:
        values["limit"], values["offset"] = page_size, (page - 1) * page_size
        return conn.execute(query, values).fetchall()

    page = params.page
    rows = fetch(page)
    if rows:
        total, other_total = rows[0][-2:]
    else:
        # An empty page: nothing matches, or the page is past the end (a stale link).
        counts = conn.execute(
            sql.SQL(
                "SELECT count(*) FILTER (WHERE group_state = ANY(%(view_states)s)), "
                "count(*) FILTER (WHERE group_state = ANY(%(other_states)s)) FROM ({}) o"
            ).format(grouped),
            values,
        ).fetchone()
        total, other_total = counts
        last_page = max(1, -(-total // page_size))
        if page > last_page:
            page = last_page
            rows = fetch(page)
    pages = max(1, -(-total // page_size))
    extractions = {}
    if params.skills and rows:
        extractions = dict(conn.execute(
            sql.SQL("SELECT posting_id, extraction FROM {}.srv_postings "
                    "WHERE posting_id = ANY(%s)").format(sql.Identifier(schema)),
            ([row[1] for row in rows],),
        ).fetchall())  # fmt: skip
    result_rows = [
        ResultRow(
            *row[:6],
            sorted({loc for loc in row[6] if loc}),
            *row[7:29],
            skill_notes=_skill_notes(extractions.get(row[1]), params.skills),
            is_new=bool(row[29]),
        )
        for row in rows
    ]
    return SearchResult(replace(params, page=page), result_rows, total, other_total, page,
                        pages, page_size, list(problems or []))  # fmt: skip


def group_members(
    conn: psycopg.Connection, posting_id: int, schema: str = SERVING_SCHEMA
) -> list[int]:
    """The listings counted as one job with this one (its confident duplicate group), or just
    the listing itself."""
    rows = conn.execute(
        sql.SQL(
            "SELECT posting_id FROM {s}.srv_postings WHERE opportunity_key = (SELECT "
            "opportunity_key FROM {s}.srv_postings WHERE posting_id = %s) ORDER BY posting_id"
        ).format(s=sql.Identifier(schema)),
        (posting_id,),
    ).fetchall()
    return [r[0] for r in rows] or [posting_id]


def load_choices(conn: psycopg.Connection, schema: str = SERVING_SCHEMA) -> Choices:
    """Employers with listings, and every skill some listing asks for."""
    _check_serving(conn, schema)
    srv = sql.Identifier(schema)
    employers = dict(conn.execute(
        sql.SQL("SELECT DISTINCT employer_id, employer_name FROM {}.srv_postings "
                "ORDER BY employer_name").format(srv)).fetchall())  # fmt: skip
    skills = conn.execute(
        sql.SQL(
            "SELECT DISTINCT skill FROM {}.srv_postings, unnest(requested_skills) skill "
            "ORDER BY skill"
        ).format(srv)
    ).fetchall()
    return Choices(employers, {s.lower(): s for (s,) in skills})


def popular_skills(
    conn: psycopg.Connection, population: str, schema: str = SERVING_SCHEMA, limit: int = 40
) -> list[str]:
    """Skills most often asked for among active listings, for the skills picker."""
    where = "AND retained" if population == "kept" else ""
    rows = conn.execute(
        sql.SQL(
            "SELECT skill FROM {}.srv_postings, unnest(requested_skills) skill "
            f"WHERE availability_state = ANY(%s) {where} "
            "GROUP BY skill ORDER BY count(*) DESC, skill LIMIT %s"
        ).format(sql.Identifier(schema)),
        (list(ACTIVE_STATES), limit),
    ).fetchall()
    return [s for (s,) in rows]


@dataclass(frozen=True)
class SiteStatus:
    last_complete_collection: datetime | None
    boards: int
    stale_boards: int
    held_boards: int
    listings: int
    pending_extraction: int


def site_status(
    conn: psycopg.Connection, now: datetime, schema: str = SERVING_SCHEMA
) -> SiteStatus:
    """Freshness of the whole collection, for the page header."""
    _check_serving(conn, schema)
    srv = sql.Identifier(schema)
    boards = conn.execute(
        sql.SQL(
            "SELECT max(last_complete_collected_at), count(*), "
            "count(*) FILTER (WHERE last_complete_collected_at IS NULL "
            "OR last_complete_collected_at < %s), count(*) FILTER (WHERE on_hold) "
            "FROM {}.srv_boards WHERE is_enabled"
        ).format(srv),
        (now - STALE_AFTER,),
    ).fetchone()
    listings = conn.execute(
        sql.SQL("SELECT count(*), count(*) FILTER (WHERE NOT extracted) FROM {}.srv_postings "
                "WHERE availability_state = ANY(%s)").format(srv),
        (list(ACTIVE_STATES),),
    ).fetchone()  # fmt: skip
    return SiteStatus(*boards, *listings)
