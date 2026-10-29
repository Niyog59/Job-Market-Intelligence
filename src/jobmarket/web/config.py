"""Website configuration.

The website reads the database as the read-only analytics login (jobmarket_analytics, see
jobmarket.analytics), never as the ingestion owner, and every session is also marked read
only. The database settings come from the same DB_* variables; the login from
ANALYTICS_DB_USER (default jobmarket_analytics) and ANALYTICS_DB_PASSWORD.
"""

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from jobmarket.analytics import ANALYTICS_ROLE
from jobmarket.config import Settings, load_settings
from jobmarket.search import SERVING_SCHEMA


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class WebConfig:
    settings: Settings
    serving_schema: str = SERVING_SCHEMA
    # Where the ingestion views (listings, current_extractions, ...) are found; None means
    # the login's default search path.
    source_schema: str | None = None
    clock: Callable[[], datetime] = field(default=_now)


def load_web_config(environ: Mapping[str, str] | None = None) -> WebConfig:
    """Configuration from the environment (and .env, through load_settings)."""
    settings = load_settings(environ)
    env = os.environ if environ is None else environ
    login = env.get("ANALYTICS_DB_USER", "").strip() or ANALYTICS_ROLE
    return WebConfig(
        replace(settings, db_user=login, db_password=env.get("ANALYTICS_DB_PASSWORD", ""))
    )
