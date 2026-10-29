"""Run the dbt analytics project (analytics/) and set up its read-only database login.

dbt reads the ingestion schema and builds three schemas of its own: analytics_staging
(views over ingestion tables), analytics_intermediate (shared rules), and
analytics_reporting (tables for the website and Tableau). A rebuild drops and recreates only
those schemas' tables and views; ingestion tables are never written. On a normal install the
build runs as the jobmarket_analytics login, which can only read ingestion tables.
"""

import os
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql

PROJECT_DIR = Path(__file__).resolve().parents[2] / "analytics"
ANALYTICS_ROLE = "jobmarket_analytics"


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


GRANTS = (
    "GRANT CONNECT ON DATABASE {db} TO {role}",
    "GRANT USAGE ON SCHEMA {source} TO {role}",
    "GRANT SELECT ON ALL TABLES IN SCHEMA {source} TO {role}",
    # Tables and views added by later migrations (created by the owner) are readable too.
    "ALTER DEFAULT PRIVILEGES IN SCHEMA {source} GRANT SELECT ON TABLES TO {role}",
    # Lets the login create (and own) its analytics_* schemas, nothing else.
    "GRANT CREATE ON DATABASE {db} TO {role}",
)


def _require_role(conn: psycopg.Connection) -> None:
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ANALYTICS_ROLE,))
    if exists.fetchone() is None:
        raise AnalyticsUnavailable(
            f"the {ANALYTICS_ROLE} login does not exist yet; a superuser must create it first "
            f"(CREATE ROLE {ANALYTICS_ROLE} LOGIN PASSWORD '...')"
        )


def grant_read_access(conn: psycopg.Connection, source_schema: str = "public") -> list[str]:
    """Give the analytics login read access to ingestion tables and the right to create its
    own schemas. Run by the database owner; safe to repeat. The login itself is created
    once by a superuser (CREATE ROLE ... LOGIN PASSWORD ...)."""
    _require_role(conn)
    database = conn.execute("SELECT current_database()").fetchone()[0]
    done = []
    for template in GRANTS:
        statement = sql.SQL(template).format(
            db=sql.Identifier(database),
            role=sql.Identifier(ANALYTICS_ROLE),
            source=sql.Identifier(source_schema),
        )
        conn.execute(statement)
        done.append(statement.as_string(conn))
    return done


def read_only_check(conn: psycopg.Connection, source_schema: str = "public") -> dict[str, bool]:
    """What the analytics login may do in the ingestion schema (all write rights must be
    False)."""
    _require_role(conn)
    checks = {}
    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
        combine = "bool_and" if privilege == "SELECT" else "bool_or"  # all reads, any write
        checks[privilege] = conn.execute(
            f"SELECT {combine}(has_table_privilege(%s, format('%%I.%%I', schemaname, "
            "tablename), %s)) FROM pg_tables WHERE schemaname = %s",
            (ANALYTICS_ROLE, privilege, source_schema),
        ).fetchone()[0]
    checks["CREATE in ingestion schema"] = conn.execute(
        "SELECT has_schema_privilege(%s, %s, 'CREATE')", (ANALYTICS_ROLE, source_schema)
    ).fetchone()[0]
    return checks
