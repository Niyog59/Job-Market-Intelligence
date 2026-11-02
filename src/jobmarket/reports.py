"""Read-only access to the reporting tables (analytics_reporting) for the public Reports page.

The reporting tables change only when `jobmarket analytics build` runs, unlike the serving views
behind job search, so every load also returns rpt_build_info: when the build ran and the newest
collection and extraction it included. Nothing here writes or recomputes a measure; the
definitions live in the dbt models.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import psycopg
from psycopg import sql

REPORTING_SCHEMA = "analytics_reporting"

# The page uses the search page's names ("kept"); the reporting models call it "retained".
POPULATIONS = {"kept": "retained", "all": "all"}

# Fixed display order for availability states (the reporting model's values).
_STATE_ORDER = ["observed", "possibly_unavailable", "assumed_inactive", "closed", "not_tracked"]
ELIGIBILITY_CONCEPTS = ["sponsorship", "work_authorization", "citizenship", "clearance"]
EXPERIENCE_MEASURES = ["early_career", "overall_min_years", "seniority", "graduation"]
_YEAR_BUCKETS = ["0", "1", "2", "3-4", "5-7", "8+", "not_stated", "not_extracted"]

CONCEPT_LABELS = {
    "sponsorship": "Visa sponsorship",
    "work_authorization": "Work authorization",
    "citizenship": "Citizenship",
    "clearance": "Security clearance",
}
MEASURE_LABELS = {
    "early_career": "Open to early career",
    "overall_min_years": "Smallest required overall experience",
    "seniority": "Seniority",
    "graduation": "Graduation requirement",
}
VALUE_LABELS = {
    "not_extracted": "No structured details yet",
    "not_stated": "Not stated",
    "not_asked": "Not recorded",
    "ambiguous": "Unclear",
    "unclear": "Unclear",
    # sponsorship / authorization / citizenship / clearance
    "supported": "Sponsorship offered",
    "not_supported": "No sponsorship",
    "required": "Required",
    "preferred": "Preferred",
    "must_be_eligible": "Must be eligible",
    "us_citizen_required": "U.S. citizenship required",
    "us_person_required": "U.S. person status required",
    "other_stated": "Other requirement stated",
    # experience
    "yes": "Early career",
    "no": "Not early career",
    "internship": "Internship",
    "stated": "Stated",
    "0": "Under 1 year",
    "1": "1 year",
    "2": "2 years",
    "3-4": "3 to 4 years",
    "5-7": "5 to 7 years",
    "8+": "8 or more years",
}
STATE_LABELS = {
    "observed": "Observed",
    "possibly_unavailable": "Possibly unavailable",
    "assumed_inactive": "Assumed inactive",
    "closed": "Closed",
    "not_tracked": "Imported before availability tracking",
}


def label(value: str | None) -> str:
    if value is None:
        return "None"
    return VALUE_LABELS.get(value, value.replace("_", " ").capitalize())


class ReportsUnavailable(Exception):
    """The reporting tables do not exist yet (analytics have not been built)."""


@dataclass(frozen=True)
class BuildInfo:
    built_at: datetime
    latest_complete_collection: datetime | None
    latest_extraction: datetime | None


@dataclass
class Reports:
    population: str  # "kept" or "all"
    build: BuildInfo
    activity: list[dict[str, Any]]
    inventory_day: date | None
    inventory_complete: bool
    inventory: list[dict[str, Any]]
    coverage: dict[str, Any] | None
    skills: list[dict[str, Any]]
    eligibility: dict[str, list[dict[str, Any]]]
    experience: dict[str, list[dict[str, Any]]]
    health: list[dict[str, Any]]
    boards: list[dict[str, Any]]


def _rows(conn: psycopg.Connection, query: sql.Composable, params=()) -> list[dict[str, Any]]:
    cur = conn.execute(query, params)
    names = [c.name for c in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def _grouped(rows: list[dict[str, Any]], key: str, order: list[str]) -> dict[str, list]:
    groups: dict[str, list] = {name: [] for name in order}
    for row in rows:
        groups.setdefault(row[key], []).append(row)
    return {name: items for name, items in groups.items() if items}


def load_reports(
    conn: psycopg.Connection, population: str = "kept", schema: str = REPORTING_SCHEMA
) -> Reports:
    """Every report for one population. Raises ReportsUnavailable before the first build."""
    if population not in POPULATIONS:
        population = "kept"
    pop = POPULATIONS[population]
    found = conn.execute("SELECT to_regclass(%s)", (f'"{schema}".rpt_build_info',)).fetchone()
    if found[0] is None:
        raise ReportsUnavailable("the reports have not been built yet")

    def table(name: str) -> sql.Composable:
        return sql.Identifier(schema, name)

    build = BuildInfo(*conn.execute(
        sql.SQL("SELECT built_at, latest_complete_collection, latest_extraction FROM {}")
        .format(table("rpt_build_info"))).fetchone())  # fmt: skip

    activity = _rows(conn, sql.SQL(
        "SELECT * FROM {} WHERE population = %s ORDER BY report_day DESC"
    ).format(table("rpt_daily_activity")), (pop,))  # fmt: skip

    latest = conn.execute(sql.SQL(
        "SELECT report_day, bool_and(is_complete) FROM {} WHERE population = %s "
        "GROUP BY report_day ORDER BY report_day DESC LIMIT 1"
    ).format(table("rpt_inventory_daily")), (pop,)).fetchone()  # fmt: skip
    inventory_day, inventory_complete = latest if latest else (None, True)
    inventory = _rows(conn, sql.SQL(
        "SELECT availability_state, coverage, source_records, opportunities, seen_that_day, "
        "carried_forward FROM {} WHERE population = %s AND report_day = %s "
        "ORDER BY array_position(%s::text[], availability_state)"
    ).format(table("rpt_inventory_daily")), (pop, inventory_day, _STATE_ORDER))  # fmt: skip

    coverage = next(iter(_rows(conn, sql.SQL("SELECT * FROM {} WHERE population = %s").format(
        table("rpt_extraction_coverage")), (pop,))), None)  # fmt: skip

    # One row per skill: alone-required, required as one of several choices, and preferred are
    # separate columns because one posting can appear in more than one of them.
    skills = _rows(conn, sql.SQL(
        "SELECT skill, "
        "  coalesce(sum(postings) FILTER (WHERE requirement = 'required' "
        "    AND mention_kind = 'individual'), 0) AS required, "
        "  coalesce(sum(postings) FILTER (WHERE requirement = 'required' "
        "    AND mention_kind = 'alternative'), 0) AS required_choice, "
        "  coalesce(sum(postings) FILTER (WHERE requirement = 'preferred'), 0) AS preferred, "
        "  max(extracted_postings) AS extracted_postings "
        "FROM {} WHERE population = %s AND requirement IN ('required', 'preferred') "
        "GROUP BY skill ORDER BY required DESC, required_choice DESC, preferred DESC, skill"
    ).format(table("rpt_skill_demand")), (pop,))  # fmt: skip

    eligibility = _grouped(_rows(conn, sql.SQL(
        "SELECT * FROM {} WHERE population = %s ORDER BY concept, postings DESC, status"
    ).format(table("rpt_eligibility")), (pop,)), "concept", ELIGIBILITY_CONCEPTS)  # fmt: skip

    experience_rows = _rows(conn, sql.SQL(
        "SELECT * FROM {} WHERE population = %s ORDER BY measure, postings DESC, value"
    ).format(table("rpt_experience_requirements")), (pop,))  # fmt: skip
    experience = _grouped(experience_rows, "measure", EXPERIENCE_MEASURES)
    if "overall_min_years" in experience:
        experience["overall_min_years"].sort(
            key=lambda row: (
                _YEAR_BUCKETS.index(row["value"])
                if row["value"] in _YEAR_BUCKETS
                else len(_YEAR_BUCKETS)
            )
        )

    health = _rows(conn, sql.SQL("SELECT * FROM {} ORDER BY report_day DESC").format(
        table("rpt_pipeline_health_daily")))  # fmt: skip

    boards = _rows(conn, sql.SQL(
        "SELECT employer_name, source_system, latest_scan_status, last_complete_collected_at, "
        "hours_since_complete_collection, on_hold FROM {} WHERE is_enabled "
        "ORDER BY hours_since_complete_collection DESC NULLS FIRST, employer_name"
    ).format(table("rpt_board_freshness")))  # fmt: skip

    return Reports(
        population=population,
        build=build,
        activity=activity,
        inventory_day=inventory_day,
        inventory_complete=inventory_complete,
        inventory=inventory,
        coverage=coverage,
        skills=skills,
        eligibility=eligibility,
        experience=experience,
        health=health,
        boards=boards,
    )


def failed_rebuild_since(conn: psycopg.Connection, built_at) -> datetime | None:
    """When the most recent report rebuild failed, if it failed after the published build
    was made (the page then says it shows the last good reports). None if there is no
    failure since, or the pipeline record does not exist (before migration 0014)."""
    if conn.execute("SELECT to_regclass('pipeline_step_runs')").fetchone()[0] is None:
        return None
    row = conn.execute(
        "SELECT finished_at, status FROM pipeline_step_runs WHERE step = 'report_publish' "
        "ORDER BY finished_at DESC, step_run_id DESC LIMIT 1"
    ).fetchone()
    if row and row[1] == "failed" and (built_at is None or row[0] > built_at):
        return row[0]
    return None
