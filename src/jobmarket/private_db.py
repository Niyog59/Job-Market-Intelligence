"""The private login (jobmarket_private): the only login the website uses for private data.

It may read and write the private schema and nothing else; the analytics login may not even
see that schema. The owner (the migration login) grants these rights with
`jobmarket private grant` and verifies them with `jobmarket private check-access`. The login
itself is created once by a superuser (in pgAdmin, or CREATE ROLE ... LOGIN PASSWORD ...).
"""

import psycopg
from psycopg import sql

from jobmarket.analytics import ANALYTICS_ROLE
from jobmarket.migrate import current_private_schema

PRIVATE_ROLE = "jobmarket_private"

GRANTS = (
    "GRANT CONNECT ON DATABASE {db} TO {role}",
    "GRANT USAGE ON SCHEMA {private} TO {role}",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {private} TO {role}",
    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {private} TO {role}",
    # Private tables added by later migrations (created by the owner) are covered too.
    "ALTER DEFAULT PRIVILEGES IN SCHEMA {private} "
    "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA {private} GRANT USAGE, SELECT ON SEQUENCES TO {role}",
    # Belt and braces: the analytics login gets nothing here.
    "REVOKE ALL ON SCHEMA {private} FROM {analytics}",
)


class PrivateLoginMissing(Exception):
    pass


def _role_exists(conn: psycopg.Connection, role: str) -> bool:
    return conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is not None


def grant_private_access(conn: psycopg.Connection) -> list[str]:
    """Run by the database owner; safe to repeat."""
    if not _role_exists(conn, PRIVATE_ROLE):
        raise PrivateLoginMissing(
            f"the {PRIVATE_ROLE} login does not exist yet; a superuser must create it first "
            f"(CREATE ROLE {PRIVATE_ROLE} LOGIN PASSWORD '...')"
        )
    database = conn.execute("SELECT current_database()").fetchone()[0]
    names = {
        "db": sql.Identifier(database),
        "role": sql.Identifier(PRIVATE_ROLE),
        "private": sql.Identifier(current_private_schema(conn)),
        "analytics": sql.Identifier(ANALYTICS_ROLE),
    }
    done = []
    for template in GRANTS:
        if "{analytics}" in template and not _role_exists(conn, ANALYTICS_ROLE):
            continue
        statement = sql.SQL(template).format(**names)
        conn.execute(statement)
        done.append(statement.as_string(conn))
    return done


def access_report(conn: psycopg.Connection) -> dict[str, bool | None]:
    """What each login may do. Expected: the private login uses the private schema only; the
    analytics login has no access to it. None means the login does not exist."""
    private = current_private_schema(conn)
    source = conn.execute("SELECT current_schema()").fetchone()[0]
    report: dict[str, bool | None] = {}

    def has(role, check, *args):
        if not _role_exists(conn, role):
            return None
        return conn.execute(f"SELECT {check}", (role, *args)).fetchone()[0]

    def all_tables(role, schema, privilege):
        if not _role_exists(conn, role):
            return None
        return conn.execute(
            "SELECT coalesce(bool_or(has_table_privilege(%s, format('%%I.%%I', schemaname, "
            "tablename), %s)), false) FROM pg_tables WHERE schemaname = %s",
            (role, privilege, schema),
        ).fetchone()[0]

    report["private login: uses private schema"] = has(
        PRIVATE_ROLE, "has_schema_privilege(%s, %s, 'USAGE')", private
    )
    report["private login: writes any ingestion table"] = all_tables(PRIVATE_ROLE, source, "INSERT")
    report["private login: reads any ingestion table"] = all_tables(PRIVATE_ROLE, source, "SELECT")
    report["analytics login: uses private schema"] = has(
        ANALYTICS_ROLE, "has_schema_privilege(%s, %s, 'USAGE')", private
    )
    report["analytics login: reads any private table"] = all_tables(
        ANALYTICS_ROLE, private, "SELECT"
    )
    return report


EXPECTED = {
    "private login: uses private schema": True,
    "private login: writes any ingestion table": False,
    "private login: reads any ingestion table": False,
    "analytics login: uses private schema": False,
    "analytics login: reads any private table": False,
}
