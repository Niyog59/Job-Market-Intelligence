"""PostgreSQL connections."""

from dataclasses import dataclass

import psycopg

from jobmarket.config import Settings

CONNECT_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class ServerInfo:
    database: str
    user: str
    server_version: str


def connect(settings: Settings, **overrides) -> psycopg.Connection:
    """Open a new connection. The caller is responsible for closing it.

    A short connect timeout makes an unreachable server fail fast instead of hanging.
    Extra keyword arguments are passed to psycopg.connect() (for example ``options``).
    """
    kwargs = settings.connection_kwargs()
    kwargs.setdefault("connect_timeout", CONNECT_TIMEOUT_SECONDS)
    kwargs.setdefault("application_name", "jobmarket")
    kwargs.update(overrides)
    return psycopg.connect(**kwargs)


def check_connection(settings: Settings) -> ServerInfo:
    """Connect, run ``SELECT 1``, and report which server and database answered.

    Raises psycopg.Error if the server cannot be reached, rejects the login, or the
    query does not return 1.
    """
    with connect(settings) as conn:
        row = conn.execute("SELECT 1").fetchone()
        if row is None or row[0] != 1:
            raise psycopg.DatabaseError(f"SELECT 1 returned {row!r}")
        database, user, version = conn.execute(
            "SELECT current_database(), current_user, current_setting('server_version')"
        ).fetchone()
    return ServerInfo(database=database, user=user, server_version=version)
