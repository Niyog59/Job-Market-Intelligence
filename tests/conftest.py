"""Shared fixtures.

Database tests use the DB_* settings (environment, then .env). When no database is
configured or reachable they are skipped, unless JOBMARKET_REQUIRE_DB=1 (set in CI),
in which case they fail so a broken database setup cannot pass silently.

Tests that change the schema work inside a temporary PostgreSQL schema that is dropped
afterwards, so existing tables and data in the configured database are never touched.
"""

import os
import uuid
from collections.abc import Iterator

import httpx
import psycopg
import pytest
from gh_fakes import FakeClock, ScriptedTransport, board, registry
from search_world import world  # noqa: F401  (fixture shared by the search and web tests)

from jobmarket import db
from jobmarket.collector import Collector
from jobmarket.config import ConfigError, Settings, load_settings
from jobmarket.migrate import apply_migrations
from jobmarket.sources import sync_registry


def _database_unavailable(reason: str):
    if os.environ.get("JOBMARKET_REQUIRE_DB") == "1":
        pytest.fail(f"JOBMARKET_REQUIRE_DB=1 but {reason}")
    pytest.skip(reason)


@pytest.fixture(scope="session")
def db_settings() -> Settings:
    try:
        settings = load_settings()
    except ConfigError as exc:
        _database_unavailable(f"database not configured: {exc}")
    try:
        db.check_connection(settings)
    except psycopg.Error as exc:
        _database_unavailable(f"database not reachable: {exc}")
    return settings


@pytest.fixture
def isolated_schema(db_settings: Settings) -> Iterator[str]:
    """Create an empty schema for one test and drop it (and everything in it) afterwards."""
    schema = f"test_{uuid.uuid4().hex[:12]}"
    with db.connect(db_settings, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    try:
        yield schema
    finally:
        with db.connect(db_settings, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
def schema_conn(db_settings: Settings, isolated_schema: str) -> Iterator[psycopg.Connection]:
    """An autocommit connection whose unqualified names resolve to the temporary schema.

    Autocommit means every statement commits on its own, so a constraint violation in
    one statement does not abort the rest of the test.
    """
    with db.connect(
        db_settings, autocommit=True, options=f"-c search_path={isolated_schema}"
    ) as conn:
        yield conn


@pytest.fixture
def env(schema_conn, tmp_path):
    """A migrated schema with two enabled boards (acme, beta), a scripted HTTP transport,
    and a controlled clock. Raw responses are written under tmp_path."""
    apply_migrations(schema_conn)
    sync_registry(
        schema_conn, registry(board("acme"), board("beta", employer="Beta")), FakeClock()()
    )
    transport = ScriptedTransport()
    clock = FakeClock()

    class Env:
        conn = schema_conn
        raw_root = tmp_path / "raw"

        def __init__(self):
            self.transport, self.clock = transport, clock

        def collector(self):
            # A new Collector each time, as after a restart: all state is in the database.
            client = httpx.Client(transport=self.transport)
            return Collector(
                self.conn, client, self.raw_root, clock=self.clock, sleep=lambda s: None, interval=0
            )

        def one(self, sql, params=()):
            return self.conn.execute(sql, params).fetchone()

        def scalar(self, sql, params=()):
            return self.one(sql, params)[0]

    return Env()


@pytest.fixture(autouse=True)
def no_live_gemini(monkeypatch):
    """Tests never call Gemini: creating a real client fails the test at once."""
    from jobmarket import gemini

    def refuse(*args, **kwargs):
        raise RuntimeError("tests must not create a live Gemini client; use a scripted model")

    monkeypatch.setattr(gemini.GeminiClient, "__init__", refuse)
