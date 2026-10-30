"""Explainable comparison of a job's stored requirements with the owner's confirmed profile.

Inputs are the stored AI answer for the listing's CURRENT description version (Module 7) and
the latest saved profile; nothing calls the model. Each check is Matched, Gap, or Needs
review, with a plain explanation and the listing's own quotes. There is no score.

- A skill missing from the profile is "not listed in your profile", never a gap: the profile
  is not a complete inventory of what someone knows. "Python or Java" is met by either.
- Definite gaps only for explicit, reliably comparable mismatches: years of employment below a
  stated minimum, an education level below a stated level, a graduation date outside a stated
  window, and stated preferences (labeled "outside your preferences", decision 2026-10-29).
  Anything missing, ambiguous, or not faithfully comparable is Needs review.
- Required years count employment only, overlapping months once; internships do not count
  but turn a shortfall into Needs review; projects never count (decision 2026-10-29).
- Alternative paths ("a degree or equivalent experience") are met if any path is met;
  otherwise they need review rather than becoming a gap.
- Sponsorship, work authorization, citizenship, and clearance are shown as statements only.
  Nothing is inferred about the owner's eligibility, and silence is not support or refusal.
"""

from dataclasses import dataclass, field
from datetime import date

from jobmarket.search import PLACE_CHOICES
from jobmarket.skills import canonical_skill
from jobmarket.web.format import ARRANGEMENT_LABELS, EMPLOYMENT_LABELS, join_natural, words
from jobmarket.web.pages import Quote, found_quotes, merged_quotes, quotes_for
from jobmarket.workspace import Profile, today

MATCHED, GAP, REVIEW = "matched", "gap", "needs_review"
RESULT_LABELS = {MATCHED: "Matched", GAP: "Gap", REVIEW: "Needs review"}
# Levels that can be ordered; certificates, bootcamps, and "other" cannot.
LEVEL_RANK = {"high_school": 1, "associate": 2, "bachelor": 3, "master": 4, "doctorate": 5}
ROLE_NAMES = {
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
    "uncertain": "Unclear role",
}


@dataclass(frozen=True)
class Check:
    section: str
    requirement: str
    result: str
    explanation: str
    quotes: tuple[Quote, ...] = ()


@dataclass(frozen=True)
class Statement:
    label: str
    value: str
    quotes: tuple[Quote, ...]
    highlighted: bool


@dataclass
class Comparison:
    available: bool
    reason: str = ""
    checks: list[Check] = field(default_factory=list)
    statements: list[Statement] = field(default_factory=list)
    profile_version: int = 0
    description_version: int = 0

    def count(self, result: str) -> int:
        return sum(1 for c in self.checks if c.result == result)


def _key(name: str) -> str:
    return canonical_skill(name)[0].lower()


def _months(value: date) -> int:
    return value.year * 12 + value.month


def employment_months(profile: Profile, now_month: date) -> int:
    """Months of employment, each calendar month counted once however many jobs overlap."""
    months: set[int] = set()
    for e in profile.experiences:
        if e.kind != "employment" or e.start is None:
            continue
        end = now_month if e.current or e.end is None else e.end
        months.update(range(_months(e.start), _months(end) + 1))
    return len(months)


def _duration(months: int) -> str:
    years, rest = divmod(months, 12)
    parts = ([f"{years} year{'s' * (years != 1)}"] if years else []) + (
        [f"{rest} month{'s' * (rest != 1)}"] if rest or not years else []
    )
    return " ".join(parts)


def _skill_checks(n, skills: set[str], found) -> list[Check]:
    checks = []
    grouped = {_key(m["canonical"]) for g in n["skill_groups"] for m in g["members"]}
    for status, section in (("required", "Required skills"), ("preferred", "Preferred skills")):
        for g in [g for g in n["skill_groups"] if g["status"] == status]:
            members = [m["canonical"] for m in g["members"]]
            have = [m for m in members if _key(m) in skills]
            quotes = quotes_for(g, found)
            if g["relation"] == "any_of":
                if have:
                    checks.append(
                        Check(
                            section,
                            g["display_text"],
                            MATCHED,
                            f"{join_natural(have, 'and')} satisfies "
                            f"“{g['display_text']}”; one of them is "
                            "enough.",
                            quotes,
                        )
                    )
                else:
                    checks.append(
                        Check(
                            section,
                            g["display_text"],
                            REVIEW,
                            "None of these is listed in your profile. That does "
                            "not mean you lack them.",
                            quotes,
                        )
                    )
            elif g["relation"] == "at_least":
                need = g["min_count"] or 1
                result = MATCHED if len(have) >= need else REVIEW
                checks.append(
                    Check(
                        section,
                        g["display_text"],
                        result,
                        f"Needs at least {need}; your profile lists {len(have)}"
                        f"{': ' + join_natural(have) if have else ''}.",
                        quotes,
                    )
                )
            else:
                missing = [m for m in members if _key(m) not in skills]
                checks.append(
                    Check(
                        section,
                        g["display_text"],
                        REVIEW if missing else MATCHED,
                        "All are listed in your profile."
                        if not missing
                        else f"Not listed in your profile: {join_natural(missing)}.",
                        quotes,
                    )
                )
        for s in [
            s for s in n["skills"] if s["status"] == status and _key(s["canonical"]) not in grouped
        ]:
            listed = _key(s["canonical"]) in skills
            checks.append(
                Check(
                    section,
                    s["canonical"],
                    MATCHED if listed else REVIEW,
                    "Listed in your profile."
                    if listed
                    else "Not listed in your profile. That does not mean you lack it.",
                    quotes_for(s, found),
                )
            )
    for s in [s for s in n["skills"] if s["status"] == "ambiguous"]:
        listed = _key(s["canonical"]) in skills
        checks.append(
            Check(
                "Skills, level unclear",
                s["canonical"],
                REVIEW,
                ("Listed in your profile, but " if listed else "Not listed in your profile, and ")
                + "the listing does not say whether it is required.",
                quotes_for(s, found),
            )
        )
    return checks


def _experience_check(item, profile: Profile, now_month: date, found) -> Check:
    status = item["status"]
    label = _years_text(item) + (f" with {item['subject']}" if item["subject"] else "")
    quotes = quotes_for(item, found)
    section = "Experience" if status != "preferred" else "Experience (preferred)"
    if item["scope"] != "overall":
        return Check(
            section,
            label,
            REVIEW,
            "Experience with a specific skill or domain cannot "
            "be measured from your profile; check your entries.",
            quotes,
        )
    if item["min_years"] is None:
        return Check(section, label, REVIEW, "The listing states no minimum to compare.", quotes)
    months = employment_months(profile, now_month)
    need = item["min_years"] * 12
    total = f"Your listed employment totals {_duration(months)} (overlapping months counted once)"
    if months >= need:
        return Check(section, label, MATCHED, f"{total}.", quotes)
    if status == "ambiguous":
        return Check(
            section,
            label,
            REVIEW,
            f"{total}; the listing is unclear whether this is required.",
            quotes,
        )
    if any(e.kind == "internship" for e in profile.experiences):
        return Check(
            section,
            label,
            REVIEW,
            f"{total}. Internships are listed but do not count "
            "toward required years; check how the employer counts them.",
            quotes,
        )
    note = "" if status == "required" else " This is preferred, not required."
    return Check(
        section,
        label,
        GAP,
        f"{total}; the listing asks for "
        f"{item['min_years']:g}+ years.{note} Projects are not counted.",
        quotes,
    )


def _years_text(item) -> str:
    lo, hi = item.get("min_years"), item.get("max_years")
    if lo is not None and hi is not None:
        return f"{lo:g}–{hi:g} years"
    if lo is not None:
        return f"{lo:g}+ years"
    if hi is not None:
        return f"Up to {hi:g} years"
    return "Experience"


def _education_check(item, profile: Profile, found) -> Check:
    level = item["level"]
    fields = item["fields"]
    label = words(level).capitalize() + (f" in {join_natural(fields, 'or')}" if fields else "")
    quotes = quotes_for(item, found)
    section = "Education" if item["status"] != "preferred" else "Education (preferred)"
    if level not in LEVEL_RANK:
        return Check(
            section,
            label,
            REVIEW,
            "This kind of credential cannot be compared by level; check the listing.",
            quotes,
        )
    entries = [e for e in profile.education if e.level in LEVEL_RANK]
    if not profile.education:
        return Check(section, label, REVIEW, "No education is listed in your profile.", quotes)
    completed = [
        e for e in entries if e.status == "completed" and LEVEL_RANK[e.level] >= LEVEL_RANK[level]
    ]
    expected = [
        e for e in entries if e.status == "expected" and LEVEL_RANK[e.level] >= LEVEL_RANK[level]
    ]

    def field_note(e) -> str:
        if not fields:
            return ""
        if e.field and any(
            f.lower() in e.field.lower() or e.field.lower() in f.lower() for f in fields
        ):
            return f" Your field ({e.field}) matches."
        return f" Check the field: the listing names {join_natural(fields, 'or')}" + (
            f"; your entry says {e.field}." if e.field else "; your entry names none."
        )

    if completed:
        e = completed[0]
        note = field_note(e)
        result = MATCHED if not note or "matches" in note else REVIEW
        return Check(
            section,
            label,
            result,
            f"Your completed {words(e.level)} meets the level.{note}",
            quotes,
        )
    if expected:
        e = expected[0]
        when = f"{e.graduation:%B %Y}" if e.graduation else "a future date"
        return Check(
            section,
            label,
            REVIEW,
            f"Your {words(e.level)} is expected in {when}. "
            "Check whether the employer accepts an expected degree."
            f"{field_note(e)}",
            quotes,
        )
    if entries and len(entries) == len(profile.education):
        if item["status"] == "ambiguous":
            return Check(
                section,
                label,
                REVIEW,
                "Your listed education is below this level, "
                "and the listing is unclear whether it is required.",
                quotes,
            )
        note = "" if item["status"] == "required" else " This is preferred, not required."
        return Check(
            section, label, GAP, f"Your listed education is below this level.{note}", quotes
        )
    return Check(
        section,
        label,
        REVIEW,
        "Some of your education cannot be compared by level; check the listing.",
        quotes,
    )


def _graduation_check(n, profile: Profile, found) -> Check | None:
    g = n["graduation"]
    if g["status"] == "not_stated":
        return None
    quotes = quotes_for(g, found)
    if g["status"] == "ambiguous":
        return Check(
            "Graduation",
            "Graduation date",
            REVIEW,
            "The listing's graduation requirement is unclear.",
            quotes,
        )
    window = f"{g['earliest'] or 'any time'} to {g['latest'] or 'any time'}"
    months = [e.graduation for e in profile.education if e.graduation]
    if not months:
        return Check(
            "Graduation", window, REVIEW, "No graduation month is listed in your profile.", quotes
        )

    def inside(d: date) -> bool:
        text = f"{d.year:04d}-{d.month:02d}"
        return (not g["earliest"] or text >= g["earliest"]) and (
            not g["latest"] or text <= g["latest"]
        )

    hit = [d for d in months if inside(d)]
    if hit:
        return Check(
            "Graduation",
            window,
            MATCHED,
            f"Your graduation month ({hit[0]:%B %Y}) falls within the stated window.",
            quotes,
        )
    return Check(
        "Graduation",
        window,
        GAP,
        "None of your graduation months falls within the stated window.",
        quotes,
    )


def _preference_checks(n, derived, profile: Profile, found) -> list[Check]:
    checks = []
    if profile.preferred_places:
        wanted = {v for k in profile.preferred_places for v in PLACE_CHOICES[k][1]}
        labels = join_natural([PLACE_CHOICES[k][0] for k in profile.preferred_places], "or")
        place = derived.get("place")
        quotes = merged_quotes(
            *n["locations"], n["work_arrangement"], *n["remote_restrictions"], found=found
        )
        if place in wanted:
            checks.append(
                Check(
                    "Preferences",
                    "Location",
                    MATCHED,
                    f"Within your preferred locations ({labels}).",
                    quotes,
                )
            )
        elif place in (None, "unknown") or (
            place == "remote_ambiguous" and "remote_texas" in profile.preferred_places
        ):
            checks.append(
                Check(
                    "Preferences",
                    "Location",
                    REVIEW,
                    "The listing does not make clear where the work can be done.",
                    quotes,
                )
            )
        else:
            checks.append(
                Check(
                    "Preferences", "Location", GAP, f"Outside your preferences ({labels}).", quotes
                )
            )
    for attr, key, labels, section in (
        ("preferred_arrangements", "work_arrangement", ARRANGEMENT_LABELS, "Work arrangement"),
        ("preferred_employment", "employment_type", EMPLOYMENT_LABELS, "Employment type"),
    ):
        wanted = getattr(profile, attr)
        if not wanted:
            continue
        claim = n[key]
        value = claim["value"]
        quotes = quotes_for(claim, found)
        names = join_natural([labels.get(w, words(w)) for w in wanted], "or")
        if value in wanted:
            checks.append(
                Check(
                    "Preferences", section, MATCHED, f"{labels.get(value)}, as you prefer.", quotes
                )
            )
        elif value in ("not_stated", "ambiguous", "multiple"):
            checks.append(
                Check(
                    "Preferences",
                    section,
                    REVIEW,
                    f"The listing "
                    f"{'offers several' if value == 'multiple' else 'does not state it clearly'}"
                    f"; you prefer {names}.",
                    quotes,
                )
            )
        else:
            checks.append(
                Check(
                    "Preferences",
                    section,
                    GAP,
                    f"{labels.get(value, words(value))}: outside your preferences ({names}).",
                    quotes,
                )
            )
    if profile.preferred_roles:
        cats = derived.get("role_categories") or []
        names = join_natural([ROLE_NAMES[r] for r in profile.preferred_roles], "or")
        quotes = merged_quotes(*n["role_categories"], found=found)
        if set(cats) & set(profile.preferred_roles):
            checks.append(
                Check(
                    "Preferences",
                    "Role family",
                    MATCHED,
                    f"In your preferred role families ({names}).",
                    quotes,
                )
            )
        elif not cats or set(cats) <= {"uncertain"}:
            checks.append(
                Check(
                    "Preferences",
                    "Role family",
                    REVIEW,
                    "The listing does not make the role family clear.",
                    quotes,
                )
            )
        else:
            shown = join_natural([ROLE_NAMES.get(c, c) for c in cats])
            checks.append(
                Check(
                    "Preferences",
                    "Role family",
                    GAP,
                    f"{shown}: outside your preferences ({names}).",
                    quotes,
                )
            )
    return checks


_STATEMENTS = (
    (
        "sponsorship",
        "Sponsorship",
        {
            "supported": "The listing says it sponsors visas.",
            "not_supported": "The listing says it does not sponsor visas.",
            "ambiguous": "The sponsorship wording is unclear.",
        },
    ),
    (
        "work_authorization",
        "Work authorization",
        {
            "required": "The listing requires authorization to work.",
            "ambiguous": "The authorization wording is unclear.",
        },
    ),
    (
        "citizenship",
        "Citizenship",
        {
            "us_citizen_required": "U.S. citizenship is required.",
            "us_person_required": "U.S. person status is required.",
            "other_stated": "A citizenship requirement is stated.",
            "ambiguous": "The citizenship wording is unclear.",
        },
    ),
    (
        "clearance",
        "Clearance",
        {
            "required": "A security clearance is required.",
            "preferred": "A security clearance is preferred.",
            "must_be_eligible": "You must be eligible for a security clearance.",
            "ambiguous": "The clearance wording is unclear.",
        },
    ),
)


def _statements(n, profile: Profile, found) -> list[Statement]:
    found_any = []
    for key, label, texts in _STATEMENTS:
        claim = n[key]
        if claim["status"] == "not_stated":
            continue
        highlighted = (
            profile.highlight_sponsorship
            if key == "sponsorship"
            else profile.highlight_restrictions
        )
        found_any.append(
            Statement(label, texts[claim["status"]], quotes_for(claim, found), highlighted)
        )
    return found_any


def compare(listing, profile: Profile, evidence: list[dict] | None, now) -> Comparison:
    """The comparison for one listing (jobmarket.listings.ListingDetail)."""
    extraction = listing.extraction
    if profile.version == 0:
        return Comparison(False, "Save your profile to see a comparison.")
    if extraction.status != "extracted" or not extraction.normalized:
        reason = {
            "failed": "the AI could not read this version",
            "gave_up": "the AI could not read this version",
            "skipped": "this listing cannot be sent to the AI",
        }.get(extraction.status, "the AI has not read this version of the listing yet")
        return Comparison(
            False,
            f"No comparison: {reason}. Nothing is compared against an "
            "older or missing description.",
            profile_version=profile.version,
            description_version=listing.version_no,
        )
    n = extraction.normalized
    derived = getattr(extraction, "derived", None) or {}
    found = found_quotes(evidence)
    skills = {_key(s.name) for s in profile.skills}
    now_month = today(now).replace(day=1)
    checks = _skill_checks(n, skills, found)
    alternatives = {a["group"]: a for a in n["alternatives"]}
    by_group: dict[str, list[Check]] = {}
    for item in n["experience"]:
        check = _experience_check(item, profile, now_month, found)
        if item.get("alternative_group") in alternatives:
            by_group.setdefault(item["alternative_group"], []).append(check)
        else:
            checks.append(check)
    for item in n["education"]:
        check = _education_check(item, profile, found)
        if item.get("alternative_group") in alternatives:
            by_group.setdefault(item["alternative_group"], []).append(check)
        else:
            checks.append(check)
    for group, paths in by_group.items():
        alt = alternatives[group]
        met = [p for p in paths if p.result == MATCHED]
        if met:
            checks.append(
                Check(
                    "Experience or education",
                    alt["display_text"],
                    MATCHED,
                    f"One accepted path is met: {met[0].requirement}. {met[0].explanation}",
                    quotes_for(alt, found),
                )
            )
        else:
            detail = " ".join(f"{p.requirement}: {p.explanation}" for p in paths)
            checks.append(
                Check(
                    "Experience or education",
                    alt["display_text"],
                    REVIEW,
                    f"Any one path is accepted; none is clearly met. {detail}",
                    quotes_for(alt, found),
                )
            )
    graduation = _graduation_check(n, profile, found)
    if graduation:
        checks.append(graduation)
    checks += _preference_checks(n, derived, profile, found)
    return Comparison(
        True,
        checks=checks,
        statements=_statements(n, profile, found),
        profile_version=profile.version,
        description_version=listing.version_no,
    )
