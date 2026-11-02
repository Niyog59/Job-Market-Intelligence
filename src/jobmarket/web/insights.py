"""The Market insights page: Tableau Public dashboards embedded in the site, plus the
headline numbers on the job-search page.

The dashboards are built in Tableau Desktop Public Edition from the Google Sheet that
`jobmarket tableau sync` fills, and published to Tableau Public. Each one is embedded by its
view address, https://public.tableau.com/views/<workbook>/<view>, so the dashboard tabs in
the workbook must carry the view names below (docs/tableau/BUILD_GUIDE.md). Until
JOBMARKET_TABLEAU_WORKBOOK is set the page explains that the dashboards are on their way and
shows the headline numbers from the database.

The headline numbers read the published table rpt_jobs_current, so they match the
dashboards' latest publication.
"""

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

import psycopg
from psycopg import sql

log = logging.getLogger(__name__)

TABLEAU_HOST = "https://public.tableau.com"
WORKBOOK_ENV = "JOBMARKET_TABLEAU_WORKBOOK"  # the workbook's name in its Tableau Public address
PROFILE_ENV = "JOBMARKET_TABLEAU_PROFILE"  # the owner's Tableau Public profile address


@dataclass(frozen=True)
class Dashboard:
    slug: str
    title: str
    view: str  # the dashboard tab's name in the workbook's address
    question: str
    objective: str  # the decision the dashboard supports


DASHBOARDS = (
    Dashboard(
        "main", "What are employers asking for?", "WhatEmployersAskFor",
        "Which skills, levels, degrees, and experience do DFW and Houston employers require?",
        "Decide which skills to build first and which roles are realistic to apply for now.",
    ),
    Dashboard(
        "levels", "Junior vs senior hiring", "JuniorVsSenior",
        "Is hiring for junior roles growing or shrinking compared with mid and senior roles?",
        "Know whether entry-level openings are expanding, to time applications and set "
        "expectations.",
    ),
    Dashboard(
        "ai", "AI in the job market", "AIInTheJobMarket",
        "How is generative AI showing up in job requirements, including outside AI roles?",
        "See whether AI skills are becoming expected in ordinary data and software roles.",
    ),
    Dashboard(
        "skills", "Skills in depth", "SkillsInDepth",
        "How do skills differ between data and software roles, and which come together?",
        "Plan learning as the bundles of skills employers ask for together.",
    ),
    Dashboard(
        "market", "Market overview", "MarketOverview",
        "Where are the jobs, who is hiring, and how is the number of open jobs changing?",
        "Focus applications on the places, work arrangements, and employers with the most "
        "openings.",
    ),
    Dashboard(
        "requirements", "Requirements and eligibility", "Requirements",
        "What experience, degrees, and work eligibility do employers require?",
        "Spot early the jobs you are not eligible for, and see how common each requirement is.",
    ),
    Dashboard(
        "data", "About the data", "AboutTheData",
        "How fresh and complete is this data, and what are its limits?",
        "Judge how much weight each number can carry.",
    ),
)  # fmt: skip
BY_SLUG = {d.slug: d for d in DASHBOARDS}


@dataclass(frozen=True)
class TableauConfig:
    workbook: str | None
    profile: str | None


def load_tableau_config(environ: Mapping[str, str] | None = None) -> TableauConfig:
    env = os.environ if environ is None else environ
    workbook = env.get(WORKBOOK_ENV, "").strip() or None
    profile = env.get(PROFILE_ENV, "").strip() or None
    if profile and not profile.startswith(f"{TABLEAU_HOST}/"):
        profile = None  # only ever link to Tableau Public
    return TableauConfig(workbook, profile)


def view_url(config: TableauConfig, dashboard: Dashboard) -> str | None:
    """The dashboard's own page on Tableau Public, or None before the workbook is published."""
    if not config.workbook:
        return None
    return (
        f"{TABLEAU_HOST}/views/{quote(config.workbook, safe='')}/{quote(dashboard.view, safe='')}"
    )


def embed_url(config: TableauConfig, dashboard: Dashboard) -> str | None:
    """The address to show in the page's frame: the view without Tableau Public's own page
    around it, tabs hidden (each dashboard has its own page here)."""
    view = view_url(config, dashboard)
    if view is None:
        return None
    return f"{view}?:embed=y&:showVizHome=no&:tabs=no&:toolbar=yes&:display_count=no"


@dataclass(frozen=True)
class Headline:
    jobs: int
    employers: int
    senior_share: float  # of jobs with a known level (Junior, Mid, Senior+)
    junior_share: float
    genai_share: float  # of all current jobs
    built_at: datetime
    collected_through: datetime | None


def headline(conn: psycopg.Connection, schema: str) -> Headline | None:
    """Headline numbers from the published reports, or None when there are none to show."""
    query = sql.SQL(
        "SELECT count(*), count(DISTINCT employer_name), "
        "count(*) FILTER (WHERE level_ai = 'Senior+'), "
        "count(*) FILTER (WHERE level_ai = 'Junior'), "
        "count(*) FILTER (WHERE level_ai IN ('Junior', 'Mid', 'Senior+')), "
        "count(*) FILTER (WHERE genai_mention), "
        "(SELECT built_at FROM {s}.rpt_build_info), "
        "(SELECT latest_complete_collection FROM {s}.rpt_build_info) "
        "FROM {s}.rpt_jobs_current").format(s=sql.Identifier(schema))  # fmt: skip
    try:
        # A savepoint, so a missing table leaves the page's other queries working.
        with conn.transaction():
            row = conn.execute(query).fetchone()
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
        # Nothing published yet, or the published reports predate the dashboard tables.
        return None
    except psycopg.errors.InsufficientPrivilege:
        # The numbers are an extra on job search, which must keep working without them.
        log.warning("cannot read %s.rpt_jobs_current for the headline numbers", schema)
        return None
    jobs, employers, senior, junior, known, genai, built_at, collected = row
    if not jobs:
        return None

    def share(n: int) -> float:
        return n / known if known else 0.0

    return Headline(jobs, employers, share(senior), share(junior), genai / jobs, built_at,
                    collected)  # fmt: skip
