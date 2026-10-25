"""Application settings loaded from environment variables.

Values are read from the process environment. A .env file in the current working
directory (or a parent directory) is loaded first if present; variables already set in
the environment take precedence over the file, so CI and production can override it.
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from dotenv import find_dotenv, load_dotenv

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 5432

# ASCII digits only: int() alone would also accept "+5432", "5_432", and non-ASCII digits.
_PORT_PATTERN = re.compile(r"[0-9]{1,5}")


class ConfigError(Exception):
    """Raised when a required setting is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str = field(default="", repr=False)

    def connection_kwargs(self) -> dict[str, Any]:
        """Return keyword arguments for psycopg.connect().

        An empty password is omitted so libpq can fall back to ~/.pgpass or
        password-less local authentication.
        """
        kwargs: dict[str, Any] = {
            "host": self.db_host,
            "port": self.db_port,
            "dbname": self.db_name,
            "user": self.db_user,
        }
        if self.db_password:
            kwargs["password"] = self.db_password
        return kwargs


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Return database settings from the environment.

    DB_HOST      optional, defaults to "localhost"
    DB_PORT      optional, defaults to 5432, must be a whole number from 1 to 65535
    DB_NAME      required
    DB_USER      required
    DB_PASSWORD  optional, defaults to ""

    If ``environ`` is given, it is used instead of the process environment and no .env
    file is read. Empty or whitespace-only values count as missing.

    Raises ConfigError listing every missing or invalid value. Error messages never
    include the password.
    """
    if environ is None:
        load_dotenv(find_dotenv(usecwd=True))
        environ = os.environ

    def get(name: str) -> str:
        return environ.get(name, "").strip()

    problems: list[str] = []

    host = get("DB_HOST") or DEFAULT_HOST

    port = DEFAULT_PORT
    raw_port = get("DB_PORT")
    if raw_port:
        if _PORT_PATTERN.fullmatch(raw_port) and 1 <= int(raw_port) <= 65535:
            port = int(raw_port)
        else:
            problems.append(f"DB_PORT must be a whole number from 1 to 65535, got {raw_port!r}")

    name = get("DB_NAME")
    if not name:
        problems.append("DB_NAME is required")

    user = get("DB_USER")
    if not user:
        problems.append("DB_USER is required")

    if problems:
        raise ConfigError("; ".join(problems))

    # Not stripped: leading or trailing spaces can be part of a real password.
    password = environ.get("DB_PASSWORD", "")

    return Settings(
        db_host=host,
        db_port=port,
        db_name=name,
        db_user=user,
        db_password=password,
    )
