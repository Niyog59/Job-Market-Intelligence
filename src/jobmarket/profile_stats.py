"""The public numbers file (profile-stats.json) behind the GitHub profile and README updaters.

After a successful publication, build() reads the published reporting tables (the same ones
the Reports page reads) and write() replaces the file atomically. Every number is validated
first; on any problem a ProfileStatsError is raised and the previous file stays as it was,
so the updaters keep the last good numbers.

Fields (names fixed: the updaters read them):
  boards_enabled            enabled boards (rpt_board_freshness)
  open_jobs                 currently listed kept jobs, duplicates merged (rpt_jobs_current)
  employers_with_open_jobs  distinct employers among them
  kept_jobs_total           kept opportunities in every availability state on the newest
                            inventory day (rpt_inventory_daily, population 'retained')
  extracted_jobs            listed kept postings with structured details, of
  extracted_total           all listed kept postings (rpt_extraction_coverage)
  junior_pct, senior_pct    shares of kept postings whose AI-read seniority is stated
                            (rpt_experience_requirements, measure 'seniority')
  genai_pct                 share of open jobs that mention generative AI (rpt_job_genai_terms)
  last_published_at         the build's date in America/Chicago (rpt_build_info), YYYY-MM-DD
"""

import json
import os
import tempfile
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql

from jobmarket.reports import REPORTING_SCHEMA

PUBLIC_DIR_ENV = "JOBMARKET_PUBLIC_DIR"
DEFAULT_PUBLIC_DIR = Path("data/public")
FILE_NAME = "profile-stats.json"
ZONE = ZoneInfo("America/Chicago")
POPULATION = "retained"  # kept jobs, as on the Reports page's default view

FIELDS = (
    "boards_enabled", "open_jobs", "employers_with_open_jobs", "kept_jobs_total",
    "extracted_jobs", "extracted_total", "junior_pct", "senior_pct", "genai_pct",
    "last_published_at",
)  # fmt: skip

# Seniority values in rpt_experience_requirements (the extraction schema's SENIORITY, plus
# not_extracted). Junior and senior follow the dashboards' level grouping; mid counts toward
# the stated total only. A value outside these sets stops the build.
JUNIOR = {"intern", "entry", "junior"}
MID = {"mid"}
SENIOR = {"senior", "staff", "principal", "lead", "manager", "director", "executive"}
NOT_STATED = {"not_stated", "ambiguous", "not_extracted"}


class ProfileStatsError(Exception):
    """The numbers could not be built or failed validation; nothing was written."""


def public_dir() -> Path:
    return Path(os.environ.get(PUBLIC_DIR_ENV) or DEFAULT_PUBLIC_DIR)


def stats_path() -> Path:
    return public_dir() / FILE_NAME


def _percent(part: int, whole: int) -> int:
    if not whole:
        return 0
    return int((Decimal(100) * part / whole).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def build(conn: psycopg.Connection, schema: str = REPORTING_SCHEMA) -> dict[str, Any]:
    """The ten numbers from the published reporting tables, validated."""
    s = sql.Identifier(schema)

    def one(query: str, params=()) -> tuple:
        return conn.execute(sql.SQL(query).format(s=s), params).fetchone()

    try:
        (boards,) = one("SELECT count(*) FROM {s}.rpt_board_freshness WHERE is_enabled")
        open_jobs, employers = one(
            "SELECT count(*), count(DISTINCT employer_name) FROM {s}.rpt_jobs_current"
        )
        (kept_total,) = one(
            "SELECT coalesce(sum(opportunities), 0) FROM {s}.rpt_inventory_daily "
            "WHERE population = %(p)s AND report_day = (SELECT max(report_day) "
            "FROM {s}.rpt_inventory_daily WHERE population = %(p)s)",
            {"p": POPULATION},
        )
        coverage = one(
            "SELECT extracted, listed_postings FROM {s}.rpt_extraction_coverage "
            "WHERE population = %s",
            (POPULATION,),
        )
        seniority = conn.execute(
            sql.SQL(
                "SELECT value, postings FROM {s}.rpt_experience_requirements "
                "WHERE measure = 'seniority' AND population = %s"
            ).format(s=s),
            (POPULATION,),
        ).fetchall()
        (genai_jobs,) = one("SELECT count(DISTINCT job_key) FROM {s}.rpt_job_genai_terms")
        built = one("SELECT built_at FROM {s}.rpt_build_info")
    except psycopg.Error as exc:
        raise ProfileStatsError(f"cannot read the published reports: {exc}") from exc

    if coverage is None:
        raise ProfileStatsError("rpt_extraction_coverage has no row for kept jobs")
    if built is None or built[0] is None:
        raise ProfileStatsError("rpt_build_info has no build time")
    unknown = sorted({value for value, _ in seniority} - JUNIOR - MID - SENIOR - NOT_STATED)
    if unknown:
        raise ProfileStatsError(f"unrecognized seniority value(s): {', '.join(map(str, unknown))}")
    junior = sum(n for value, n in seniority if value in JUNIOR)
    senior = sum(n for value, n in seniority if value in SENIOR)
    stated = junior + senior + sum(n for value, n in seniority if value in MID)

    stats = {
        "boards_enabled": int(boards),
        "open_jobs": int(open_jobs),
        "employers_with_open_jobs": int(employers),
        "kept_jobs_total": int(kept_total),
        "extracted_jobs": int(coverage[0]),
        "extracted_total": int(coverage[1]),
        "junior_pct": _percent(junior, stated),
        "senior_pct": _percent(senior, stated),
        "genai_pct": _percent(genai_jobs, open_jobs),
        "last_published_at": built[0].astimezone(ZONE).date().isoformat(),
    }
    validate(stats)
    return stats


def validate(stats: dict[str, Any]) -> None:
    """Raise ProfileStatsError unless every field is present and the numbers are possible."""
    missing = [name for name in FIELDS if name not in stats]
    if missing:
        raise ProfileStatsError(f"missing field(s): {', '.join(missing)}")
    counts = [name for name in FIELDS if name != "last_published_at"]
    for name in counts:
        value = stats[name]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ProfileStatsError(f"{name} must be a whole number of at least 0, not {value!r}")
    if stats["boards_enabled"] == 0:
        raise ProfileStatsError("no enabled boards")
    if stats["employers_with_open_jobs"] > stats["open_jobs"]:
        raise ProfileStatsError("more employers than open jobs")
    if stats["kept_jobs_total"] < stats["open_jobs"]:
        raise ProfileStatsError("kept_jobs_total is below open_jobs")
    if stats["extracted_jobs"] > stats["extracted_total"]:
        raise ProfileStatsError("extracted_jobs is above extracted_total")
    for name in ("junior_pct", "senior_pct", "genai_pct"):
        if stats[name] > 100:
            raise ProfileStatsError(f"{name} is above 100")


def write(stats: dict[str, Any], path: Path | None = None) -> Path:
    """Validate, then replace the file in one step: readers see the old file or the new one,
    never a partly written one."""
    validate(stats)
    path = path or stats_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({name: stats[name] for name in FIELDS}, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)  # the website reads it as another user
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def refresh(conn: psycopg.Connection, schema: str = REPORTING_SCHEMA,
            path: Path | None = None) -> dict[str, Any]:  # fmt: skip
    """Build from the published reports and write the file; nothing is written on error."""
    stats = build(conn, schema)
    write(stats, path)
    return stats
