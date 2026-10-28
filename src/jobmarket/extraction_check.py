"""Checks and clean-up applied in Python to every model answer.

check_answer() runs three layers:

1. Structure. The answer must match RESPONSE_SCHEMA exactly: types, allowed values, required
   fields, no extra fields, numeric bounds. Any violation makes the answer invalid and the
   request is retried; nothing from it is stored as a result.
2. Consistency and evidence. Relationships must refer to listed skills, alternative groups
   must exist, year ranges must be ordered, dates must look like YYYY-MM, and every quote is
   searched for in the exact document that was sent. Problems here are warnings: the
   answer is kept (decision 2026-10-27: "we trust the AI"), and each quote records whether
   it was found and where. A claim with no evidence, or whose quotes were not found, is
   marked so in the evidence list. Passing these checks shows the answer is well formed and
   quoted from the posting; it does not show the answer is right.
3. Normalization. Skill aliases and locations are tidied deterministically
   (jobmarket.skills, jobmarket.places), and comparison fields are derived for the review
   of disagreements with rule-based screening. Re-running this layer on stored answers
   needs no model call.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from jobmarket.extraction_schema import RESPONSE_SCHEMA, TARGET_CATEGORIES
from jobmarket.places import country_name, normalize_location, state_code
from jobmarket.skills import canonical_skill

NORMALIZATION_VERSION = "2026-10-28.2"  # .2: Canadian provinces, "GB"

# Same set as rule-based screening treats as available in DFW/Houston or to Texas residents.
RETAINED_PLACES = ("local_dfw", "local_houston", "local_both", "remote_texas", "remote_ambiguous")


@dataclass
class CheckedAnswer:
    valid: bool
    errors: list[str] = field(default_factory=list)  # structure problems (invalid)
    warnings: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    normalized: dict | None = None
    derived: dict | None = None

    @property
    def status(self) -> str:
        return "valid_with_warnings" if self.warnings else "valid"


# --- 1. structure ---------------------------------------------------------------------------

_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
    "null": type(None),
}


def _type_ok(value: Any, kind: str) -> bool:
    if kind in ("integer", "number") and isinstance(value, bool):
        return False
    if kind == "integer" and isinstance(value, float):
        return value.is_integer()
    return isinstance(value, _TYPES[kind])


def schema_errors(value: Any, schema: dict = RESPONSE_SCHEMA, path: str = "$") -> list[str]:
    kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    if not any(_type_ok(value, k) for k in kinds):
        return [f"{path}: expected {' or '.join(kinds)}, got {type(value).__name__}"]
    if value is None:
        return []
    errors: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not an allowed value")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} is below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} is above {schema['maximum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}: missing {name}")
        for name, item in value.items():
            if name not in props:
                errors.append(f"{path}: unexpected field {name}")
            else:
                errors += schema_errors(item, props[name], f"{path}.{name}")
    if isinstance(value, list):
        for i, item in enumerate(value):
            errors += schema_errors(item, schema["items"], f"{path}[{i}]")
    return errors


# --- 2. evidence ------------------------------------------------------------------------------

_TYPOGRAPHY = str.maketrans(
    {"‘": "'", "’": "'", "‚": "'", "‛": "'", "“": '"', "”": '"', "„": '"', "‐": "-",
     "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-", "…": "..."}
)  # fmt: skip


def _fold(text: str) -> tuple[str, list[int]]:
    """Case-folded, typography-folded text with runs of whitespace collapsed, and for each
    folded character the offset of the original character it came from."""
    out: list[str] = []
    origin: list[int] = []
    space = False
    for i, ch in enumerate(text):
        piece = unicodedata.normalize("NFKC", ch).translate(_TYPOGRAPHY).lower()
        for c in piece:
            if c.isspace():
                if not space and out:
                    out.append(" ")
                    origin.append(i)
                space = True
            else:
                out.append(c)
                origin.append(i)
                space = False
    return "".join(out), origin


class Document:
    """The exact text sent to the model, searchable for quotes."""

    def __init__(self, text: str):
        self.text = text
        self.folded, self.origin = _fold(text)

    def locate(self, quote: str) -> list[tuple[int, int]] | None:
        """Character spans in the original text for a quote, or None if it is not there.
        A quote elided with '...' must have every part present, in order."""
        folded = _fold(quote)[0].strip()
        if not folded:
            return None
        candidates = [folded, folded.rstrip(".,;:!? ")]
        for candidate in candidates:
            at = self.folded.find(candidate)
            if candidate and at >= 0:
                return [self._span(at, len(candidate))]
        parts = [p.strip(" .,;:") for p in re.split(r"\.\.\.+", folded)]
        parts = [p for p in parts if p]
        if len(parts) > 1:
            spans, at = [], 0
            for part in parts:
                found = self.folded.find(part, at)
                if found < 0:
                    return None
                spans.append(self._span(found, len(part)))
                at = found + len(part)
            return spans
        return None

    def _span(self, at: int, length: int) -> tuple[int, int]:
        return self.origin[at], self.origin[at + length - 1] + 1


def _claims(answer: dict):
    """(path, claim, needs_evidence) for every object that carries evidence."""
    for key in ("seniority", "employment_type"):
        yield key, answer[key], answer[key]["value"] != "not_stated"
    for key in ("sponsorship", "work_authorization", "citizenship", "clearance", "graduation"):
        yield key, answer[key], answer[key]["status"] != "not_stated"
    yield (
        "work_arrangement",
        answer["work_arrangement"],
        (answer["work_arrangement"]["value"] != "not_stated"),
    )
    for key in ("role_categories", "skills", "skill_groups", "experience", "education",
                "alternatives", "locations", "remote_restrictions"):  # fmt: skip
        for i, item in enumerate(answer[key]):
            yield f"{key}[{i}]", item, True


def _consistency(answer: dict) -> list[dict]:
    warnings = []
    names = {canonical_skill(s["name"])[0] for s in answer["skills"]}
    for i, group in enumerate(answer["skill_groups"]):
        path = f"skill_groups[{i}]"
        members = group["members"]
        missing = [m for m in members if canonical_skill(m)[0] not in names]
        if missing:
            warnings.append({"path": path, "issue": "member not listed in skills",
                             "detail": missing})  # fmt: skip
        if group["relation"] == "at_least":
            n = group["min_count"]
            if n is None or n > len(members):
                warnings.append({"path": path, "issue": "at_least needs a min_count no larger "
                                 "than the number of members"})  # fmt: skip
        elif len(members) < 2:
            warnings.append({"path": path, "issue": "a relationship needs at least two members"})
    groups = {a["group"] for a in answer["alternatives"]}
    used = set()
    for key in ("experience", "education"):
        for i, item in enumerate(answer[key]):
            label = item.get("alternative_group")
            if label is not None:
                used.add(label)
                if label not in groups:
                    warnings.append({"path": f"{key}[{i}]", "issue": "alternative group not "
                                     "described in alternatives", "detail": label})  # fmt: skip
    for label in sorted(groups - used):
        warnings.append({"path": "alternatives", "issue": "alternative group not used",
                         "detail": label})  # fmt: skip
    for i, item in enumerate(answer["experience"]):
        lo, hi = item["min_years"], item["max_years"]
        if lo is not None and hi is not None and lo > hi:
            warnings.append({"path": f"experience[{i}]", "issue": "min_years above max_years"})
    for key in ("earliest", "latest"):
        value = answer["graduation"][key]
        if value is not None and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
            warnings.append({"path": f"graduation.{key}", "issue": "not YYYY-MM",
                             "detail": value})  # fmt: skip
    return warnings


# --- 3. normalization ---------------------------------------------------------------------------


def _skill(item: dict) -> dict:
    canonical, mapped = canonical_skill(item["name"])
    return {"name": item["name"], "canonical": canonical, "mapped": mapped,
            "status": item["status"], "evidence": item["evidence"]}  # fmt: skip


def normalize_answer(answer: dict) -> dict:
    skills, seen = [], set()
    for item in answer["skills"]:
        skill = _skill(item)
        key = (skill["canonical"], skill["status"])
        if key not in seen:
            seen.add(key)
            skills.append(skill)
    groups = []
    for group in answer["skill_groups"]:
        members = [{"name": m, "canonical": canonical_skill(m)[0]} for m in group["members"]]
        groups.append({**group, "members": members})
    locations = [
        {"original": loc["original"],
         **normalize_location(loc["city"], loc["state"], loc["country"]),
         "evidence": loc["evidence"]}
        for loc in answer["locations"]
    ]  # fmt: skip
    restrictions = []
    for r in answer["remote_restrictions"]:
        value = r["value"]
        if r["kind"] == "country":
            value = country_name(value) or value
        elif r["kind"] == "state":
            value = state_code(value) or value
        restrictions.append({**r, "normalized_value": value})
    categories, seen_cat = [], set()
    for c in answer["role_categories"]:
        if c["category"] not in seen_cat:
            seen_cat.add(c["category"])
            categories.append(c)
    return {
        **answer,
        "role_categories": categories,
        "skills": skills,
        "skill_groups": groups,
        "locations": locations,
        "remote_restrictions": restrictions,
    }


def _place(normalized: dict) -> str:
    metros = {loc["metro"] for loc in normalized["locations"] if loc["metro"]}
    if {"dfw", "houston"} <= metros:
        return "local_both"
    if "dfw" in metros:
        return "local_dfw"
    if "houston" in metros:
        return "local_houston"
    arrangement = normalized["work_arrangement"]["value"]
    restrictions = normalized["remote_restrictions"]
    if arrangement in ("remote", "multiple"):
        states = {r["normalized_value"] for r in restrictions if r["kind"] == "state"}
        countries = {r["normalized_value"] for r in restrictions if r["kind"] == "country"}
        if "TX" in states:
            return "remote_texas"
        if states or (countries and "United States" not in countries):
            return "remote_elsewhere"
        if countries == {"United States"}:
            return "remote_texas"
        return "remote_ambiguous"  # remote with no stated limit: eligibility unknown
    if normalized["locations"]:
        return "elsewhere"
    return "unknown"


def derive(normalized: dict) -> dict:
    categories = [c["category"] for c in normalized["role_categories"]]
    relevant = any(c in TARGET_CATEGORIES for c in categories)
    place = _place(normalized)
    return {
        "role_categories": categories,
        "ai_relevant": relevant,
        "place": place,
        "ai_would_retain": relevant and place in RETAINED_PLACES,
        "sponsorship": normalized["sponsorship"]["status"],
        "work_arrangement": normalized["work_arrangement"]["value"],
    }


def check_answer(answer: Any, document: str) -> CheckedAnswer:
    errors = schema_errors(answer)
    if errors:
        return CheckedAnswer(False, errors=errors[:50])
    doc = Document(document)
    warnings = _consistency(answer)
    evidence = []
    for path, claim, needs in _claims(answer):
        quotes = claim["evidence"]
        if needs and not quotes:
            warnings.append({"path": path, "issue": "no evidence given"})
        for quote in quotes:
            spans = doc.locate(quote)
            evidence.append({"path": path, "quote": quote, "found": spans is not None,
                             "spans": spans or []})  # fmt: skip
            if spans is None:
                warnings.append({"path": path, "issue": "quote not found in the posting",
                                 "detail": quote[:200]})  # fmt: skip
    normalized = normalize_answer(answer)
    return CheckedAnswer(True, [], warnings, evidence, normalized, derive(normalized))
