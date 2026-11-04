"""Run the dbt analytics project (analytics/) and set up its read-only database login.

dbt reads the ingestion schema and builds four schemas of its own: analytics_staging
(views over ingestion tables), analytics_intermediate (shared rules), analytics_reporting
(tables for Tableau and summaries), and analytics_serving (live views for the job-search
website). A rebuild drops and recreates only those schemas' tables and views; ingestion
tables are never written. On a normal install the
build runs as the jobmarket_analytics login, which can only read ingestion tables.
"""

import os
from dataclasses import dataclass, field
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
    compiled: dict[str, str] = field(default_factory=dict)  # model name -> SQL, from compile
    timings: dict[str, float] = field(default_factory=dict)  # node name -> seconds


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
    failures, compiled, timings = [], {}, {}
    for item in getattr(result.result, "results", None) or []:
        status = str(getattr(item, "status", "")).lower()
        if status in ("error", "fail", "runtime error"):
            failures.append(f"{item.node.name}: {status} {getattr(item, 'message', '') or ''}")
        if getattr(item.node, "compiled_code", None):
            compiled[item.node.name] = item.node.compiled_code
        if getattr(item, "execution_time", None) is not None:
            timings[item.node.name] = item.execution_time
    if result.exception is not None:
        failures.append(f"dbt: {result.exception}")
    return DbtOutcome(bool(result.success), command, failures, compiled, timings)


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


# --- Building and publishing (scheduled runs) ----------------------------------------------

LAYERS = (
    "analytics_staging",
    "analytics_intermediate",
    "analytics_reporting",
    "analytics_serving",
)
SHADOW = "next__"  # where a new build is made and tested
RETIRED = "old__"  # the previous build, for the moment of the swap
PUBLISH_LOCK_KEY = 4_815_162_345
# How long dropping an old or leftover build may wait for readers still using it. Waiting
# longer would queue new website queries behind the DROP; giving up only leaves the old
# schemas for the next run to remove.
DROP_LOCK_TIMEOUT = "30s"


class PublishBusy(Exception):
    """Another build-and-publish is running."""


@dataclass
class PublishResult:
    published: bool
    failures: list[str]
    built_at: object = None  # rpt_build_info.built_at of the published build
    retired_left: list[str] = field(default_factory=list)  # old schemas still in use


def _schemas(prefix: str, kind: str) -> list[str]:
    return [f"{prefix}{kind}{layer}" for layer in LAYERS]


def _drop(conn: psycopg.Connection, names: list[str], lock_timeout: str) -> list[str]:
    """Drop schemas that exist; return those that could not be dropped in time."""
    left = []
    for name in names:
        try:
            with conn.transaction():
                conn.execute("SELECT set_config('lock_timeout', %s, true)", (lock_timeout,))
                conn.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name))
                )
        except psycopg.errors.LockNotAvailable:
            left.append(name)
    return left


def validate_build(conn: psycopg.Connection, prefix: str) -> list[str]:
    """Checks a new build must pass before it may replace the live one (beyond dbt's own
    data tests): every schema exists, the build record has exactly one row, and the views
    job search reads can be queried."""
    problems = []
    for name in _schemas(prefix, SHADOW):
        if conn.execute("SELECT to_regnamespace(%s)", (name,)).fetchone()[0] is None:
            problems.append(f"schema {name} was not built")
    if problems:
        return problems
    reporting, serving = (sql.Identifier(f"{prefix}{SHADOW}{layer}") for layer in LAYERS[2:])
    rows = conn.execute(
        sql.SQL("SELECT count(*) FROM {}.rpt_build_info").format(reporting)
    ).fetchone()[0]
    if rows != 1:
        problems.append(f"rpt_build_info has {rows} rows, expected 1")
    for view in ("srv_postings", "srv_boards"):
        try:
            with conn.transaction():
                conn.execute(sql.SQL("SELECT 1 FROM {}.{} LIMIT 1").format(
                    serving, sql.Identifier(view)))  # fmt: skip
        except psycopg.Error as exc:
            problems.append(f"{view} cannot be read: {exc}".strip())
    return problems


def _analyze(conn: psycopg.Connection, schemas: list[str]) -> None:
    """Collect planner statistics for every table in the given schemas."""
    tables = conn.execute(
        "SELECT schemaname, tablename FROM pg_tables WHERE schemaname = ANY(%s)", (schemas,)
    ).fetchall()
    for schema, table in tables:
        conn.execute(sql.SQL("ANALYZE {}.{}").format(sql.Identifier(schema), sql.Identifier(table)))


def build_and_publish(
    conn: psycopg.Connection,
    target: str = "analytics",
    source_schema: str = "public",
    prefix: str = "",
    lock_timeout: str = DROP_LOCK_TIMEOUT,
) -> PublishResult:
    """Rebuild every analytics schema in shadow schemas, test and validate the result, and
    only then replace the live schemas with it in one transaction.

    conn must be autocommit and logged in as the owner of the analytics schemas (the login
    dbt builds as). Until the swap commits, visitors keep reading the previous build; a
    failed build or validation leaves the live schemas exactly as they were and drops the
    shadow copy. Renaming a schema keeps every view pointing at the same objects, so the
    swapped-in views read the swapped-in tables.

    The swap only renames schemas, which takes no lock that a reader holds, so it never
    waits for website queries. The previous build is dropped afterwards; if a reader still
    uses it, it is left in place (retired_left) and removed by the next run.
    """
    if not conn.execute("SELECT pg_try_advisory_lock(%s)", (PUBLISH_LOCK_KEY,)).fetchone()[0]:
        raise PublishBusy("another report build is running")
    shadow, retired, live = (_schemas(prefix, SHADOW), _schemas(prefix, RETIRED),
                             _schemas(prefix, ""))  # fmt: skip
    try:
        # Leftovers of an interrupted or busy earlier attempt.
        busy = _drop(conn, shadow + retired, lock_timeout)
        if busy:
            return PublishResult(False, [f"an earlier build is still in use: {', '.join(busy)}"])
        outcome = run_dbt(
            ["build"],
            target=target,
            variables={"source_schema": source_schema, "schema_prefix": f"{prefix}{SHADOW}"},
            quiet=True,
        )
        failures = list(outcome.failures)
        if outcome.success:
            failures += validate_build(conn, prefix)
        elif not failures:
            failures.append("dbt build failed")
        if failures:
            _drop(conn, shadow, lock_timeout)
            return PublishResult(False, failures)
        # Freshly built tables have no statistics until autovacuum gets to them, which can
        # take a minute. Queries planned in that window (the website's first requests, an
        # export started right after publishing) can pick plans that run for minutes instead
        # of seconds. The tables are small, so analyzing them before the swap costs little.
        _analyze(conn, shadow)
        with conn.transaction():
            for current, old in zip(live, retired, strict=True):
                if conn.execute("SELECT to_regnamespace(%s)", (current,)).fetchone()[0]:
                    conn.execute(sql.SQL("ALTER SCHEMA {} RENAME TO {}").format(
                        sql.Identifier(current), sql.Identifier(old)))  # fmt: skip
            for new, current in zip(shadow, live, strict=True):
                conn.execute(sql.SQL("ALTER SCHEMA {} RENAME TO {}").format(
                    sql.Identifier(new), sql.Identifier(current)))  # fmt: skip
        retired_left = _drop(conn, retired, lock_timeout)
        built_at = conn.execute(
            sql.SQL("SELECT built_at FROM {}.rpt_build_info").format(
                sql.Identifier(f"{prefix}analytics_reporting"))
        ).fetchone()[0]  # fmt: skip
        return PublishResult(True, [], built_at, retired_left)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (PUBLISH_LOCK_KEY,))
