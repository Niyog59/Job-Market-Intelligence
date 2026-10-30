"""Website configuration.

Two database logins, never the ingestion owner:
- public data: the read-only analytics login (ANALYTICS_DB_USER, default jobmarket_analytics,
  and ANALYTICS_DB_PASSWORD), in read-only sessions;
- private data: the private login (PRIVATE_DB_USER, default jobmarket_private, and
  PRIVATE_DB_PASSWORD), which can use only the private schema.
Host, port, and database come from the DB_* variables.

WEB_COOKIE_SECURE (default true) marks the sign-in cookie Secure with the __Host- prefix.
Chrome and Firefox accept Secure cookies from http://127.0.0.1 and http://localhost; set it to
"false" only for a browser that does not (the cookie then loses the prefix as well).
"""

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from jobmarket.analytics import ANALYTICS_ROLE
from jobmarket.config import Settings, load_settings
from jobmarket.private_db import PRIVATE_ROLE
from jobmarket.search import SERVING_SCHEMA


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class WebConfig:
    settings: Settings
    # None: private pages are unavailable (signing in says why); browsing still works.
    private_settings: Settings | None = None
    serving_schema: str = SERVING_SCHEMA
    private_schema: str = "private"
    # Where the ingestion views (listings, current_extractions, ...) are found; None means
    # the login's default search path.
    source_schema: str | None = None
    cookie_secure: bool = True
    clock: Callable[[], datetime] = field(default=_now)


def load_web_config(environ: Mapping[str, str] | None = None) -> WebConfig:
    """Configuration from the environment (and .env, through load_settings)."""
    settings = load_settings(environ)
    env = os.environ if environ is None else environ
    login = env.get("ANALYTICS_DB_USER", "").strip() or ANALYTICS_ROLE
    private_login = env.get("PRIVATE_DB_USER", "").strip() or PRIVATE_ROLE
    private_password = env.get("PRIVATE_DB_PASSWORD", "")
    return WebConfig(
        replace(settings, db_user=login, db_password=env.get("ANALYTICS_DB_PASSWORD", "")),
        private_settings=(
            replace(settings, db_user=private_login, db_password=private_password)
            if private_password
            else None
        ),
        cookie_secure=env.get("WEB_COOKIE_SECURE", "true").strip().lower() != "false",
    )
