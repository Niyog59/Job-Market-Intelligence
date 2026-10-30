"""Database rights around private data: the private login reaches the private schema only, and
the analytics login (and so dbt and the public pages) cannot reach it at all."""

import pytest

from jobmarket.analytics import ANALYTICS_ROLE, grant_read_access
from jobmarket.migrate import apply_migrations
from jobmarket.private_db import EXPECTED, PRIVATE_ROLE, access_report, grant_private_access

pytestmark = pytest.mark.db


def _ensure_role(conn, role):
    if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
        return
    can_create = conn.execute(
        "SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname = current_user"
    ).fetchone()[0]
    if not can_create:
        pytest.skip(f"the {role} login does not exist and this login cannot create it (CI can)")
    conn.execute(f"CREATE ROLE {role} LOGIN")


def test_private_data_is_reachable_only_through_the_private_login(schema_conn, isolated_schema):
    _ensure_role(schema_conn, ANALYTICS_ROLE)
    _ensure_role(schema_conn, PRIVATE_ROLE)
    apply_migrations(schema_conn)
    grant_read_access(schema_conn, isolated_schema)
    grant_private_access(schema_conn)
    assert access_report(schema_conn) == EXPECTED
    # The ingestion schema's default grants (analytics may read new public tables) do not
    # reach the private schema.
    private = f"{isolated_schema}_private"
    schema_conn.execute(f'CREATE TABLE "{private}".later_table (i int)')
    assert access_report(schema_conn) == EXPECTED
    assert schema_conn.execute(
        "SELECT has_table_privilege(%s, %s, 'SELECT')", (PRIVATE_ROLE, f"{private}.later_table")
    ).fetchone()[0]


def test_the_analytics_project_never_reads_the_private_schema():
    from pathlib import Path

    project = Path(__file__).resolve().parents[1] / "analytics"
    sources = [p for p in project.rglob("*") if p.is_file() and p.suffix in (".sql", ".yml")
               and not {"target", "logs"} & set(p.relative_to(project).parts)]  # fmt: skip
    assert sources
    text = "\n".join(p.read_text() for p in sources)
    assert "private" not in text.lower()
