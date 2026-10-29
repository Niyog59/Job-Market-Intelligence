"""Data for the job detail page: the listing (jobmarket.listings), its serving row, the other
listings of its duplicate group, possible duplicates, closure evidence, and the extracted
requirements as labeled rows with their source quotes.

Requirement rows are built only from the extraction of the listing's CURRENT version, and
every quote shown was checked against the text of that version when the answer was stored.
When there is no extraction, no rows are built: the page says the structured details are
pending or unavailable instead of showing "not stated".
"""

from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import sql

from jobmarket.listings import ListingDetail, listing_detail
from jobmarket.web.format import ARRANGEMENT_LABELS, EMPLOYMENT_LABELS, join_natural, words

_ROLE_NAMES = {
    "data_analysis": "Data analysis", "bi": "Business intelligence",
    "data_engineering": "Data engineering", "analytics_engineering": "Analytics engineering",
    "backend": "Backend", "software_engineering": "Software engineering",
    "full_stack": "Full stack", "data_science": "Data science", "ml": "Machine learning",
    "other": "Other", "uncertain": "Unclear role",
}  # fmt: skip
_EARLY = {
    "yes": ("Early career", "Open to people early in their career."),
    "internship": ("Internship", "An internship, listed separately from early-career jobs."),
    "no": ("Not early career", "The listing asks for more experience or seniority."),
    "unclear": ("Unclear", "The listing does not make the expected level clear."),
}
_SPONSORSHIP = {
    "supported": ("Visa sponsorship offered", "The listing says it sponsors visas."),
    "not_supported": ("No visa sponsorship", "The listing says it does not sponsor visas."),
    "not_stated": ("Not stated", "No explicit sponsorship policy found."),
    "ambiguous": ("Unclear", "The sponsorship wording is unclear or contradictory."),
}
_AUTHORIZATION = {"required": "Work authorization required", "ambiguous": "Unclear"}
_CITIZENSHIP = {
    "us_citizen_required": "U.S. citizenship required",
    "us_person_required": "U.S. person status required",
    "other_stated": "Citizenship requirement stated",
    "ambiguous": "Unclear",
}
_CLEARANCE = {
    "required": "Security clearance required",
    "preferred": "Security clearance preferred",
    "must_be_eligible": "Must be eligible for a security clearance",
    "ambiguous": "Unclear",
}
_STATUS_NOTE = {"required": "Required.", "preferred": "Preferred, not mandatory.",
                "ambiguous": "Requirement level unclear."}  # fmt: skip


@dataclass(frozen=True)
class Quote:
    text: str
    found: bool  # found in the description text when the answer was checked


@dataclass(frozen=True)
class Requirement:
    label: str
    value: str
    note: str = ""
    quotes: tuple[Quote, ...] = ()
    stated: bool = True  # False for "not stated" rows (shown in muted text)


@dataclass
class JobPage:
    listing: ListingDetail
    serving: dict[str, Any]
    members: list[dict[str, Any]] = field(default_factory=list)
    possible_duplicates: list[dict[str, Any]] = field(default_factory=list)
    closure: dict[str, Any] | None = None
    requirements: list[Requirement] = field(default_factory=list)


def _quotes(claim: dict | None, found: dict[str, bool]) -> tuple[Quote, ...]:
    if not claim:
        return ()
    return tuple(Quote(q, found.get(q, True)) for q in claim.get("evidence") or [])


def _merge(*claims: dict, found: dict[str, bool]) -> tuple[Quote, ...]:
    seen, out = set(), []
    for claim in claims:
        for quote in _quotes(claim, found):
            if quote.text not in seen:
                seen.add(quote.text)
                out.append(quote)
    return tuple(out)


def _years(item: dict) -> str:
    lo, hi = item.get("min_years"), item.get("max_years")
    if lo is not None and hi is not None:
        return f"{lo:g}–{hi:g} years"
    if lo is not None:
        return f"{lo:g}+ years"
    if hi is not None:
        return f"Up to {hi:g} years"
    return "Experience"


def _skill_rows(n: dict, found: dict[str, bool]) -> list[Requirement]:
    rows = []
    grouped = {m["canonical"] for g in n["skill_groups"] for m in g["members"]}
    for status, label in (("required", "Required skills"), ("preferred", "Preferred skills")):
        groups = [g for g in n["skill_groups"] if g["status"] == status]
        alone = [s for s in n["skills"] if s["status"] == status and s["canonical"] not in grouped]
        if not groups and not alone:
            continue
        parts, notes = [], []
        if alone:
            names = [s["canonical"] for s in alone]
            parts.append(join_natural(names))
            if status == "required":
                notes.append(
                    "Both skills are required."
                    if len(names) == 2
                    else "All of these are required."
                    if len(names) > 2
                    else "Required."
                )
            else:
                notes.append("Preferred, not mandatory.")
        for g in groups:
            parts.append(g["display_text"])
            if g["relation"] == "any_of":
                notes.append(f"{g['display_text']}: any one of these is enough.")
            elif g["relation"] == "at_least":
                notes.append(f"{g['display_text']}: at least {g['min_count'] or 1} of these.")
            else:
                notes.append(f"{g['display_text']}: all of these.")
        rows.append(Requirement(label, "; ".join(parts), " ".join(notes),
                                _merge(*alone, *groups, found=found)))  # fmt: skip
    unclear = [s for s in n["skills"] if s["status"] == "ambiguous"]
    if unclear:
        rows.append(Requirement("Skills, level unclear",
                                join_natural([s["canonical"] for s in unclear]),
                                "The listing does not say whether these are required.",
                                _merge(*unclear, found=found)))  # fmt: skip
    return rows


def _experience_rows(n: dict, found: dict[str, bool]) -> list[Requirement]:
    rows = []
    alternatives = {a["group"]: a for a in n["alternatives"]}
    shown = set()
    for item in n["experience"] + n["education"]:
        group = item.get("alternative_group")
        if group in alternatives:
            if group not in shown:
                shown.add(group)
                alt = alternatives[group]
                rows.append(Requirement("Experience or education", alt["display_text"],
                                        "Either path is accepted.",
                                        _quotes(alt, found)))  # fmt: skip
            continue
        if "scope" in item:
            subject = f" with {item['subject']}" if item["subject"] else ""
            label = "Experience" if item["scope"] == "overall" else "Specific experience"
            rows.append(
                Requirement(
                    label,
                    f"{_years(item)}{subject}",
                    _STATUS_NOTE[item["status"]],
                    _quotes(item, found),
                )
            )
        else:
            fields = f" in {join_natural(item['fields'], 'or')}" if item["fields"] else ""
            rows.append(
                Requirement(
                    "Education",
                    f"{words(item['level']).capitalize()}{fields}",
                    _STATUS_NOTE[item["status"]],
                    _quotes(item, found),
                )
            )
    if not n["experience"]:
        rows.append(Requirement("Experience", "Not stated", "No years of experience found.",
                                stated=False))  # fmt: skip
    return rows  # fmt: skip


def requirement_rows(n: dict, evidence: list[dict] | None) -> list[Requirement]:
    """Labeled rows for one extraction answer (normalized), in reading order."""
    found = {}
    for item in evidence or []:
        # A quote is shown as unverified if any check of it failed.
        found[item["quote"]] = found.get(item["quote"], True) and bool(item.get("found"))
    rows = []
    cats = [_ROLE_NAMES.get(c["category"], c["category"]) for c in n["role_categories"]]
    rows.append(Requirement("Role", join_natural(cats) or "Not stated",
                            "A listing can belong to several role families." if len(cats) > 1
                            else "", _merge(*n["role_categories"], found=found),
                            stated=bool(cats)))  # fmt: skip
    early = n.get("early_career")
    if early:
        value, note = _EARLY[early["status"]]
        basis = ", ".join(words(b) for b in early["basis"])
        rows.append(
            Requirement(
                "Career level",
                value,
                f"{note}{f' Basis: {basis}.' if basis else ''}",
                _quotes(early, found),
                early["status"] != "unclear",
            )
        )
    rows += _skill_rows(n, found)
    rows += _experience_rows(n, found)
    g = n["graduation"]
    if g["status"] == "stated":
        rows.append(Requirement("Graduation", f"{g['earliest'] or 'any time'} to "
                                f"{g['latest'] or 'any time'}", "Graduation window stated.",
                                _quotes(g, found)))  # fmt: skip
    else:
        rows.append(
            Requirement(
                "Graduation",
                "Unclear" if g["status"] == "ambiguous" else "Not stated",
                "",
                _quotes(g, found),
                stated=False,
            )
        )
    value, note = _SPONSORSHIP[n["sponsorship"]["status"]]
    rows.append(Requirement("Sponsorship", value, note, _quotes(n["sponsorship"], found),
                            n["sponsorship"]["status"] != "not_stated"))  # fmt: skip
    others = []
    for key, labels, label in (("work_authorization", _AUTHORIZATION, "Work authorization"),
                               ("citizenship", _CITIZENSHIP, "Citizenship"),
                               ("clearance", _CLEARANCE, "Clearance")):  # fmt: skip
        claim = n[key]
        if claim["status"] != "not_stated":
            level = f" ({claim['level']})" if key == "clearance" and claim.get("level") else ""
            others.append(
                Requirement(
                    label,
                    labels[claim["status"]] + level,
                    "Separate from visa sponsorship.",
                    _quotes(claim, found),
                )
            )
    rows += others or [Requirement("Authorization, citizenship, clearance", "Not stated",
                                   "None of these is mentioned.", stated=False)]  # fmt: skip
    places = []
    for loc in n["locations"]:
        parts = [p for p in (loc["city"], loc["state"]) if p]
        if loc["country"] and loc["country"] not in ("United States", "US", "USA"):
            parts.append(loc["country"])
        places.append(", ".join(parts) if parts else loc["original"])
    arrangement = n["work_arrangement"]
    where = ARRANGEMENT_LABELS.get(arrangement["value"], words(arrangement["value"]))
    detail = ""
    if arrangement["onsite_days_per_week"] is not None:
        detail = f"{arrangement['onsite_days_per_week']:g} day(s) a week on site."
    elif arrangement["onsite_frequency_text"]:
        detail = f"On site: {arrangement['onsite_frequency_text']}."
    rows.append(Requirement("Location & work", " · ".join(
        [p for p in (join_natural(places), where) if p]) or "Not stated", detail,
        _merge(*n["locations"], arrangement, found=found)))  # fmt: skip
    if n["remote_restrictions"]:
        limits = [r.get("normalized_value") or r["value"] for r in n["remote_restrictions"]]
        rows.append(Requirement("Location limits", "; ".join(limits),
                                "Remote work is limited to these places.",
                                _merge(*n["remote_restrictions"], found=found)))  # fmt: skip
    employment = n["employment_type"]["value"]
    rows.append(Requirement("Employment type", EMPLOYMENT_LABELS.get(employment, words(employment)),
                            "", _quotes(n["employment_type"], found),
                            employment not in ("not_stated", "ambiguous")))  # fmt: skip
    return rows


def _rows(conn: psycopg.Connection, query: sql.Composable | str, params) -> list[dict]:
    cur = conn.execute(query, params)
    names = [c.name for c in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def job_page(conn: psycopg.Connection, posting_id: int, serving_schema: str) -> JobPage | None:
    """Everything the detail page shows for one listing, or None if it has no content."""
    srv = sql.Identifier(serving_schema)
    columns = sql.SQL(
        "posting_id, employer_name, source_system, title, location_text, availability_state, "
        "qualifying_absences, last_observed_at, posting_url, apply_url, cohort, "
        "duplicate_group_id, opportunity_key, salary_range, board_last_complete_at, "
        "board_last_trusted_check_at, board_on_hold, board_hold_opened_at, extraction_status, "
        "first_discovered_at, retained"
    )
    found = _rows(conn, sql.SQL("SELECT {} FROM {}.srv_postings WHERE posting_id = %s").format(
        columns, srv), (posting_id,))  # fmt: skip
    if not found:
        return None
    listing = listing_detail(conn, posting_id)
    if listing is None:
        return None
    serving = found[0]
    page = JobPage(listing, serving)
    if serving["duplicate_group_id"] is not None:
        page.members = _rows(conn, sql.SQL(
            "SELECT {} FROM {}.srv_postings WHERE opportunity_key = %s "
            "ORDER BY last_observed_at DESC, posting_id").format(columns, srv),
            (serving["opportunity_key"],))  # fmt: skip
    page.possible_duplicates = _rows(conn, """
        SELECT CASE WHEN f.posting_id = %(p)s THEN f.related_posting_id ELSE f.posting_id END
                   AS posting_id, f.reason, l.title, l.location_text, l.employer
        FROM posting_duplicate_flags f
        JOIN listings l ON l.posting_id =
            CASE WHEN f.posting_id = %(p)s THEN f.related_posting_id ELSE f.posting_id END
        WHERE f.status = 'open' AND %(p)s IN (f.posting_id, f.related_posting_id)
        ORDER BY 1""", {"p": posting_id})  # fmt: skip
    if listing.availability_state in ("closed", "assumed_inactive"):
        events = _rows(conn, "SELECT to_state, reason, evidence, occurred_at "
                       "FROM posting_availability_events WHERE posting_id = %s "
                       "AND to_state = %s ORDER BY occurred_at DESC LIMIT 1",
                       (posting_id, listing.availability_state))  # fmt: skip
        page.closure = events[0] if events else None
    extraction = listing.extraction
    if extraction.status == "extracted" and extraction.normalized:
        evidence = conn.execute("SELECT evidence FROM extraction_results WHERE result_id = %s",
                                (extraction.result_id,)).fetchone()  # fmt: skip
        page.requirements = requirement_rows(extraction.normalized, evidence[0] if evidence else [])
    return page
