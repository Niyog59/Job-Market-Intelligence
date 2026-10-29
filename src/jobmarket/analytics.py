"""Run the dbt analytics project (analytics/).

dbt reads the ingestion schema and builds three schemas of its own: analytics_staging
(views over ingestion tables), analytics_intermediate (shared rules), and
analytics_reporting (tables for the website and Tableau). A rebuild drops and recreates only
those schemas' tables and views; ingestion tables are never written. On a normal install the
build runs as the jobmarket_analytics login, which can only read ingestion tables.
"""

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2] / "analytics"


class AnalyticsUnavailable(Exception):
    """dbt or the analytics project is not available."""


@dataclass
class DbtOutcome:
    success: bool
    command: list[str]
    failures: list[str]


def run_dbt(
    args: list[str],
    target: str = "analytics",
    variables: dict | None = None,
    quiet: bool = False,
) -> DbtOutcome:
    """Invoke dbt in-process on the analytics project. Connection settings come from the
    environment (DB_HOST, DB_PORT, DB_NAME, and ANALYTICS_DB_PASSWORD or DB_* for --target
    owner), so nothing secret is passed on a command line."""
    if not (PROJECT_DIR / "dbt_project.yml").exists():
        raise AnalyticsUnavailable(f"no dbt project at {PROJECT_DIR}")
    try:
        from dbt.cli.main import dbtRunner
    except ImportError as exc:
        raise AnalyticsUnavailable(
            'dbt is not installed; install with: pip install -e ".[analytics]"'
        ) from exc
    import json

    command = [*args, "--project-dir", str(PROJECT_DIR), "--profiles-dir", str(PROJECT_DIR),
               "--target", target]  # fmt: skip
    if variables:
        command += ["--vars", json.dumps(variables)]
    if quiet:
        command.insert(0, "--quiet")
    os.environ.setdefault("DBT_SEND_ANONYMOUS_USAGE_STATS", "false")
    result = dbtRunner().invoke(command)
    failures = []
    for item in getattr(result.result, "results", None) or []:
        status = str(getattr(item, "status", "")).lower()
        if status in ("error", "fail", "runtime error"):
            failures.append(f"{item.node.name}: {status} {getattr(item, 'message', '') or ''}")
    if result.exception is not None:
        failures.append(f"dbt: {result.exception}")
    return DbtOutcome(bool(result.success), command, failures)
