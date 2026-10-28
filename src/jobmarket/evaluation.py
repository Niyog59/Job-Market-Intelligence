"""Evaluation of requirement extraction against labels for a fixed set of listings.

A case pins one posting version. Its file holds the exact document the model receives and
a TOML block of labels: the answer a careful reader gives for a chosen set of fields. Each
case records who stands behind its labels:

  proposed        drafted without looking at any model answer, but not checked by a person
                  (for example drafted by an AI assistant)
  human_reviewed  read and corrected by a person against the posting

Only human_reviewed labels measure accuracy. Agreement with proposed labels is reported
separately and never called accuracy.

Cases are split into practice (used while adjusting instructions) and holdout (kept unseen
until a final run). Running or reporting the holdout needs final=True, so it is not looked
at by accident. Case files contain copied posting text and stay outside Git (data/eval by
default).

Evaluation requests go through the normal queue (pacing, retries, usage limits) and their
results are ordinary results for the pinned versions.
"""

import re
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg

from jobmarket.extraction import ExtractionConfig, run_extraction, version_document
from jobmarket.places import US_STATES, country_name, normalize_location, state_code
from jobmarket.skills import canonical_skill

EVAL_DIR_ENV = "JOBMARKET_EVAL_DIR"
DEFAULT_EVAL_DIR = Path("data/eval")
REVIEW_STATES = ("proposed", "human_reviewed")
SPLITS = ("practice", "holdout")

LABEL_FIELDS = (
    "role_categories", "seniority", "employment_type", "required_skills", "preferred_skills",
    "skill_groups", "overall_min_years", "degree_or_experience_alternative", "sponsorship",
    "work_authorization", "citizenship", "clearance", "graduation", "work_arrangement",
    "locations", "remote_restrictions", "early_career",
)  # fmt: skip

TEMPLATE = """\
# Labels for this posting. Edit values only; keep the keys.
# review: "proposed" (not checked by a person) or "human_reviewed" (checked and corrected).
review = "proposed"
reviewer_notes = ""

# Role families (several allowed): data_analysis, bi, data_engineering,
# analytics_engineering, backend, software_engineering, full_stack, data_science, ml,
# other, uncertain
role_categories = []
# seniority: intern entry junior mid senior staff principal lead manager director
#   executive not_stated ambiguous
seniority = "not_stated"
# employment_type: full_time part_time contract contract_to_hire temporary internship
#   apprenticeship not_stated ambiguous
employment_type = "not_stated"

# Skills the posting requires or prefers (qualification wording, not just the tech stack).
required_skills = []
preferred_skills = []
# Relationships, e.g. { relation = "any_of", members = ["Python", "Java"], status = "required" }
# relation: all_of any_of at_least (add min_count = N for at_least)
skill_groups = []

overall_min_years = "not_stated"          # a number, or "not_stated"
# true when experience can replace a degree (or the reverse)
degree_or_experience_alternative = false

sponsorship = "not_stated"         # supported not_supported not_stated ambiguous
work_authorization = "not_stated"  # required not_stated ambiguous
# citizenship: us_citizen_required us_person_required other_stated not_stated ambiguous
citizenship = "not_stated"
clearance = "not_stated"           # required preferred must_be_eligible not_stated ambiguous
graduation = "not_stated"          # stated not_stated ambiguous

work_arrangement = "not_stated"    # onsite hybrid remote multiple not_stated ambiguous
locations = []                     # "City, ST" or as written, e.g. ["Dallas, TX"]
remote_restrictions = []           # stated limits only, e.g. ["United States"] or ["TX"]
# early_career: yes (new grad, rotational, early-career program, entry level, no experience,
#   or at most 1 year) internship no unclear
early_career = "unclear"
"""

_BLOCK = re.compile(r"```toml\n(.*?)```", re.S)


def eval_dir(environ: dict | None = None) -> Path:
    import os

    env = os.environ if environ is None else environ
    return Path(env.get(EVAL_DIR_ENV, "").strip() or DEFAULT_EVAL_DIR)


@dataclass
class Case:
    case_id: str
    posting_id: int
    version_id: int
    split: str
    bucket: str
    review: str
    labels: dict[str, Any]
    notes: str = ""
    path: Path | None = None


def write_case(directory: Path, case_id: str, posting_id: int, version_id: int, split: str,
               bucket: str, title: str, employer: str, document: str) -> Path:  # fmt: skip
    path = directory / "cases" / f"{case_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# {case_id}: {title} ({employer})\n\n"
        f"- posting_id: {posting_id}\n- version_id: {version_id}\n- split: {split}\n"
        f"- bucket: {bucket}\n\n"
        "## Labels\n\n```toml\n" + TEMPLATE + "```\n\n"
        "## Document sent to the model\n\n````text\n" + document + "````\n",
        encoding="utf-8",
    )
    return path


def prepare(conn: psycopg.Connection, directory: Path, selection: list[dict]) -> list[Path]:
    """Write one case file per selected posting (its current version), unless the file
    exists: existing labels are never overwritten."""
    written = []
    for i, item in enumerate(selection, start=1):
        case_id = f"case-{i:02d}"
        path = directory / "cases" / f"{case_id}.md"
        if path.exists():
            continue
        version_id, title, employer = conn.execute(
            "SELECT current_version_id, l.title, l.employer FROM source_postings p "
            "JOIN listings l USING (posting_id) WHERE p.posting_id = %s",
            (item["posting_id"],),
        ).fetchone()
        written.append(write_case(directory, case_id, item["posting_id"], version_id,
                                  item["split"], item["bucket"], title, employer,
                                  version_document(conn, version_id)))  # fmt: skip
    return written


_HEADER = re.compile(r"^- (posting_id|version_id|split|bucket): (.+)$", re.M)


def load_cases(directory: Path) -> list[Case]:
    cases = []
    for path in sorted((directory / "cases").glob("case-*.md")):
        text = path.read_text(encoding="utf-8")
        header = dict(_HEADER.findall(text))
        block = _BLOCK.search(text)
        if block is None:
            raise ValueError(f"{path}: no ```toml labels block")
        data = tomllib.loads(block.group(1))
        review = data.pop("review", "proposed")
        if review not in REVIEW_STATES:
            raise ValueError(f"{path}: review must be one of {REVIEW_STATES}")
        notes = data.pop("reviewer_notes", "")
        missing = [f for f in LABEL_FIELDS if f not in data]
        if missing:
            raise ValueError(f"{path}: missing labels {missing}")
        cases.append(
            Case(path.stem, int(header["posting_id"]), int(header["version_id"]),
                 header["split"], header["bucket"], review, data, notes, path)
        )  # fmt: skip
    return cases


# --- running -------------------------------------------------------------------------------


def queue_cases(conn: psycopg.Connection, config: ExtractionConfig, cases: list[Case],
                now: datetime) -> list[int]:  # fmt: skip
    """Jobs for the cases' pinned versions under the active configuration (created if
    missing). Returns the job ids that still need a result."""
    jobs = []
    for case in cases:
        row = conn.execute(
            "INSERT INTO extraction_jobs (version_id, posting_id, config_key, priority, reason, "
            "status, note, created_at, updated_at) VALUES (%s, %s, %s, 2, 'reprocess', "
            "'pending', %s, %s, %s) ON CONFLICT (version_id, config_key) DO UPDATE "
            "SET updated_at = extraction_jobs.updated_at RETURNING job_id, status",
            (case.version_id, case.posting_id, config.key, f"evaluation {case.case_id}", now,
             now),
        ).fetchone()  # fmt: skip
        if row[1] != "succeeded":
            if row[1] in ("gave_up", "superseded"):
                conn.execute(
                    "UPDATE extraction_jobs SET status = 'pending', runs_failed = 0 "
                    "WHERE job_id = %s",
                    (row[0],),
                )
            jobs.append(row[0])
    return jobs


def run_cases(conn, generate, config, clock, sleep, cases, split, final=False):
    chosen = _split(cases, split, final)
    jobs = queue_cases(conn, config, chosen, clock())
    if not jobs:
        return None
    return run_extraction(conn, generate, config, clock, sleep, only_jobs=jobs)


def _split(cases: list[Case], split: str, final: bool) -> list[Case]:
    if split not in SPLITS:
        raise ValueError(f"split must be one of {SPLITS}")
    if split == "holdout" and not final:
        raise ValueError("the holdout is kept unseen until the final run: pass final=True")
    return [c for c in cases if c.split == split]


# --- comparison --------------------------------------------------------------------------------


def _skill(name: str) -> str:
    """Canonical name for mapped skills; unmapped names compare without letter case."""
    canonical, mapped = canonical_skill(name)
    return canonical if mapped else canonical.lower()


def _skills(names) -> set[str]:
    return {_skill(n) for n in names}


def _place(value: str) -> str:
    parts = [p.strip() for p in value.split(",")]
    if len(parts) >= 2:
        loc = normalize_location(parts[0], parts[1], parts[2] if len(parts) > 2 else None)
        return f"{loc['city']}, {loc['state']}".lower()
    return value.strip().lower()


def _restriction(value: str) -> str:
    code = state_code(value)
    if code in US_STATE_CODES:
        return code
    return (country_name(value) or value).lower()


def model_view(normalized: dict) -> dict[str, Any]:
    """The labelled fields, read from a stored extraction."""
    n = normalized
    overall = [e["min_years"] for e in n["experience"]
               if e["scope"] == "overall" and e["min_years"] is not None
               and e["status"] == "required"]  # fmt: skip
    places = set()
    for loc in n["locations"]:
        if loc["city"] and loc["state"]:
            places.add(f"{loc['city']}, {loc['state']}".lower())
        elif loc["city"]:
            places.add(loc["city"].lower())
        else:
            places.add(loc["original"].strip().lower())
    # A degree-or-experience alternative: one alternative group used by both kinds of item.
    edu_groups = {e["alternative_group"] for e in n["education"] if e["alternative_group"]}
    exp_groups = {e["alternative_group"] for e in n["experience"] if e["alternative_group"]}
    return {
        "role_categories": {c["category"] for c in n["role_categories"]},
        "seniority": n["seniority"]["value"],
        "employment_type": n["employment_type"]["value"],
        "required_skills": _skills(s["name"] for s in n["skills"] if s["status"] == "required"),
        "preferred_skills": _skills(s["name"] for s in n["skills"] if s["status"] == "preferred"),
        "skill_groups": {
            (g["relation"], frozenset(_skills(m["name"] for m in g["members"])), g["status"])
            for g in n["skill_groups"]
        },
        "overall_min_years": min(overall) if overall else "not_stated",
        "degree_or_experience_alternative": bool(edu_groups & exp_groups),
        "sponsorship": n["sponsorship"]["status"],
        "work_authorization": n["work_authorization"]["status"],
        "citizenship": n["citizenship"]["status"],
        "clearance": n["clearance"]["status"],
        "graduation": n["graduation"]["status"],
        "work_arrangement": n["work_arrangement"]["value"],
        "locations": places,
        "remote_restrictions": {
            _restriction(r["normalized_value"]) for r in n["remote_restrictions"]
        },
        "early_career": (n.get("early_career") or {}).get("status", "not extracted"),
    }


def label_view(labels: dict[str, Any]) -> dict[str, Any]:
    groups = {(g["relation"], frozenset(_skills(g["members"])), g.get("status", "required"))
              for g in labels["skill_groups"]}  # fmt: skip
    years = labels["overall_min_years"]
    return {
        **{k: labels[k] for k in LABEL_FIELDS},
        "role_categories": set(labels["role_categories"]),
        "required_skills": _skills(labels["required_skills"]),
        "preferred_skills": _skills(labels["preferred_skills"]),
        "skill_groups": groups,
        "overall_min_years": years if years == "not_stated" else float(years),
        "locations": {_place(v) for v in labels["locations"]},
        "remote_restrictions": {_restriction(v) for v in labels["remote_restrictions"]},
    }


US_STATE_CODES = set(US_STATES.values())

SET_FIELDS = ("role_categories", "required_skills", "preferred_skills", "skill_groups",
              "locations", "remote_restrictions")  # fmt: skip


@dataclass
class CaseComparison:
    case: Case
    result_id: int | None
    mismatches: dict[str, tuple[Any, Any]] = field(default_factory=dict)  # field: (label, model)
    set_counts: dict[str, tuple[int, int, int]] = field(default_factory=dict)  # tp, fp, fn
    quotes_not_found: int = 0
    claims_without_evidence: int = 0
    prompt_tokens: int | None = None
    output_tokens: int | None = None


def compare_case(conn: psycopg.Connection, config: ExtractionConfig, case: Case) -> CaseComparison:
    row = conn.execute(
        "SELECT r.result_id, r.normalized, r.validation_issues, a.prompt_tokens, "
        "a.output_tokens, coalesce(a.thought_tokens, 0) FROM extraction_results r "
        "LEFT JOIN extraction_attempts a ON a.attempt_id = coalesce(r.attempt_id, "
        "(SELECT attempt_id FROM extraction_results o WHERE o.result_id = "
        "r.reused_from_result_id)) "
        "WHERE r.version_id = %s AND r.config_key = %s",
        (case.version_id, config.key),
    ).fetchone()
    comparison = CaseComparison(case, row[0] if row else None)
    if row is None:
        return comparison
    _, normalized, issues, prompt, output, thought = row
    comparison.prompt_tokens, comparison.output_tokens = prompt, (output or 0) + thought
    comparison.quotes_not_found = sum(i["issue"] == "quote not found in the posting"
                                      for i in issues)  # fmt: skip
    comparison.claims_without_evidence = sum(i["issue"] == "no evidence given" for i in issues)
    model, label = model_view(normalized), label_view(case.labels)
    for name in LABEL_FIELDS:
        if name in SET_FIELDS:
            tp = len(model[name] & label[name])
            comparison.set_counts[name] = (tp, len(model[name] - label[name]),
                                           len(label[name] - model[name]))  # fmt: skip
        if model[name] != label[name]:
            comparison.mismatches[name] = (label[name], model[name])
    return comparison


def compare(conn, config, cases, split, final=False) -> list[CaseComparison]:
    return [compare_case(conn, config, c) for c in _split(cases, split, final)]


def summarize(comparisons: list[CaseComparison]) -> dict[str, Any]:
    """Counts per field for the cases that have a result, kept apart by review state."""
    out: dict[str, Any] = {}
    for state in REVIEW_STATES:
        group = [c for c in comparisons if c.case.review == state and c.result_id is not None]
        fields = {}
        for name in LABEL_FIELDS:
            agree = sum(name not in c.mismatches for c in group)
            entry: dict[str, Any] = {"agree": agree, "cases": len(group)}
            if name in SET_FIELDS:
                tp = sum(c.set_counts[name][0] for c in group)
                fp = sum(c.set_counts[name][1] for c in group)
                fn = sum(c.set_counts[name][2] for c in group)
                entry.update(tp=tp, fp=fp, fn=fn)
            fields[name] = entry
        out[state] = {
            "cases": len(group),
            "fields": fields,
            "quotes_not_found": sum(c.quotes_not_found for c in group),
            "claims_without_evidence": sum(c.claims_without_evidence for c in group),
        }
    out["missing_results"] = [c.case.case_id for c in comparisons if c.result_id is None]
    return out
