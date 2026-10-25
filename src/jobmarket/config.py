"""Application settings loaded from environment variables.

Values are read from the process environment. A local .env file is loaded
first if present; variables already set in the environment take precedence.
"""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


class ConfigError(Exception):
    """Raised when a required setting is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str = field(default="", repr=False)


def load_settings() -> Settings:
    """Return database settings from the environment.

    DB_HOST      optional, defaults to "localhost"
    DB_PORT      optional, defaults to 5432, must be an integer
    DB_NAME      required
    DB_USER      required
    DB_PASSWORD  optional, defaults to ""

    Raises ConfigError if a required value is missing or DB_PORT is invalid.
    """
    load_dotenv()
    raise NotImplementedError
