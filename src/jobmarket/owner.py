"""The single owner account: provisioning, password checks, sessions, and sign-in throttling.

No Flask here, so the command line and the website share the same rules. Every function
takes the private schema's name (``schema``) because tests use per-test schemas.

- Passwords are hashed with Argon2id (argon2-cffi, RFC 9106 low-memory parameters, above the
  OWASP minimum) and never stored, logged, or printed.
- Sessions are server-side: the browser holds a random 256-bit token; the database holds its
  SHA-256, a CSRF token, and the idle and absolute expiry.
- Every 3 consecutive wrong passwords pause sign-in for 15 minutes (decision 2026-10-29).
  Failures are counted for the whole site, not per username, so guessing usernames gains
  nothing.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from psycopg import sql

# Decisions 2026-10-29.
IDLE_TIMEOUT = timedelta(hours=2)
ABSOLUTE_TIMEOUT = timedelta(days=7)
FAILURES_PER_PAUSE = 3
PAUSE = timedelta(minutes=15)
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 1024  # longer input is refused before hashing (no hashing-cost abuse)

_HASHER = PasswordHasher()
# Verified against when the username is wrong, so both cases take the same time.
_DUMMY_HASH = _HASHER.hash(secrets.token_urlsafe(16))


class OwnerError(Exception):
    """A provisioning or password rule was not met; the message is safe to show."""


class SignInPaused(Exception):
    def __init__(self, until: datetime):
        super().__init__(f"sign-in is paused until {until.isoformat()}")
        self.until = until


@dataclass(frozen=True)
class Session:
    session_id: int
    account_id: int
    username: str
    csrf_token: str
    created_at: datetime
    expires_at: datetime


def _t(schema: str, table: str) -> sql.Composed:
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def check_password_rules(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise OwnerError(f"the password needs at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise OwnerError(f"the password can have at most {MAX_PASSWORD_LENGTH} characters")


def owner_exists(conn: psycopg.Connection, schema: str) -> bool:
    row = conn.execute(sql.SQL("SELECT 1 FROM {}").format(_t(schema, "accounts"))).fetchone()
    return row is not None


def create_owner(
    conn: psycopg.Connection, schema: str, username: str, password: str, now: datetime
) -> None:
    """Create the one owner account. Refuses if it already exists (use reset_password)."""
    check_password_rules(password)
    if owner_exists(conn, schema):
        raise OwnerError("the owner account already exists; use `jobmarket owner reset-password`")
    try:
        conn.execute(
            sql.SQL(
                "INSERT INTO {} (account_id, username, password_hash, created_at, "
                "password_changed_at) VALUES (1, %s, %s, %s, %s)"
            ).format(_t(schema, "accounts")),
            (username, _HASHER.hash(password), now, now),
        )
    except psycopg.errors.CheckViolation as exc:
        raise OwnerError(
            "the username may use 3 to 40 letters, digits, dots, hyphens, or underscores"
        ) from exc


def reset_password(conn: psycopg.Connection, schema: str, password: str, now: datetime) -> int:
    """Recovery: set a new password, end every session, and clear the sign-in pause.
    Returns the number of sessions ended."""
    check_password_rules(password)
    with conn.transaction():
        updated = conn.execute(
            sql.SQL(
                "UPDATE {} SET password_hash = %s, password_changed_at = %s WHERE account_id = 1"
            ).format(_t(schema, "accounts")),
            (_HASHER.hash(password), now),
        ).rowcount
        if not updated:
            raise OwnerError("there is no owner account yet; use `jobmarket owner create`")
        ended = end_all_sessions(conn, schema)
        # A success marker ends the run of consecutive failures.
        conn.execute(
            sql.SQL(
                "INSERT INTO {} (attempted_at, succeeded, remote_addr) VALUES (%s, true, %s)"
            ).format(_t(schema, "login_attempts")),
            (now, "password reset"),
        )
    return ended


def end_all_sessions(conn: psycopg.Connection, schema: str) -> int:
    return conn.execute(sql.SQL("DELETE FROM {}").format(_t(schema, "sessions"))).rowcount


def paused_until(conn: psycopg.Connection, schema: str, now: datetime) -> datetime | None:
    """When sign-in reopens, if it is paused now."""
    # Consecutive failures: those recorded after the most recent success.
    failures, last_failure = conn.execute(
        sql.SQL(
            "SELECT count(*), max(attempted_at) FROM {t} WHERE NOT succeeded AND attempt_id > "
            "coalesce((SELECT max(attempt_id) FROM {t} WHERE succeeded), 0)"
        ).format(t=_t(schema, "login_attempts"))
    ).fetchone()
    if failures and failures % FAILURES_PER_PAUSE == 0:
        until = last_failure + PAUSE
        if now < until:
            return until
    return None


def _record_attempt(conn, schema, now, succeeded, remote_addr) -> None:
    conn.execute(
        sql.SQL("INSERT INTO {} (attempted_at, succeeded, remote_addr) VALUES (%s, %s, %s)").format(
            _t(schema, "login_attempts")
        ),
        (now, succeeded, remote_addr),
    )


def sign_in(
    conn: psycopg.Connection,
    schema: str,
    username: str,
    password: str,
    now: datetime,
    remote_addr: str | None = None,
) -> tuple[str, Session] | None:
    """Check the credentials and start a new session: (browser token, session), or None.
    Raises SignInPaused (without checking the password) while sign-in is paused."""
    until = paused_until(conn, schema, now)
    if until is not None:
        raise SignInPaused(until)
    row = conn.execute(
        sql.SQL("SELECT username, password_hash FROM {} WHERE account_id = 1").format(
            _t(schema, "accounts")
        )
    ).fetchone()
    stored = row[1] if row and len(password) <= MAX_PASSWORD_LENGTH else _DUMMY_HASH
    try:
        ok = _HASHER.verify(stored, password[:MAX_PASSWORD_LENGTH])
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        ok = False
    ok = ok and row is not None and hmac.compare_digest(row[0], username)
    _record_attempt(conn, schema, now, ok, remote_addr)
    if not ok:
        return None
    if _HASHER.check_needs_rehash(row[1]):
        conn.execute(
            sql.SQL("UPDATE {} SET password_hash = %s WHERE account_id = 1").format(
                _t(schema, "accounts")
            ),
            (_HASHER.hash(password),),
        )
    return start_session(conn, schema, now)


def start_session(conn: psycopg.Connection, schema: str, now: datetime) -> tuple[str, Session]:
    """A brand-new session (a new token on every sign-in, so an old one cannot be reused)."""
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    row = conn.execute(
        sql.SQL(
            "INSERT INTO {} (account_id, token_hash, csrf_token, created_at, last_seen_at, "
            "expires_at) VALUES (1, %s, %s, %s, %s, %s) RETURNING session_id"
        ).format(_t(schema, "sessions")),
        (token_hash(token), csrf, now, now, now + ABSOLUTE_TIMEOUT),
    ).fetchone()
    username = conn.execute(
        sql.SQL("SELECT username FROM {} WHERE account_id = 1").format(_t(schema, "accounts"))
    ).fetchone()[0]
    return token, Session(row[0], 1, username, csrf, now, now + ABSOLUTE_TIMEOUT)


def load_session(
    conn: psycopg.Connection, schema: str, token: str | None, now: datetime
) -> Session | None:
    """The session for a browser token, if it exists and has not expired (idle or absolute).
    An expired session is deleted. A valid one is marked as seen now."""
    if not token or len(token) > 200:
        return None
    row = conn.execute(
        sql.SQL(
            "SELECT s.session_id, s.account_id, a.username, s.csrf_token, s.created_at, "
            "s.last_seen_at, s.expires_at FROM {} s JOIN {} a USING (account_id) "
            "WHERE s.token_hash = %s"
        ).format(_t(schema, "sessions"), _t(schema, "accounts")),
        (token_hash(token),),
    ).fetchone()
    if row is None:
        return None
    session_id, account_id, username, csrf, created, last_seen, expires = row
    if now >= expires or now - last_seen >= IDLE_TIMEOUT:
        end_session(conn, schema, session_id)
        return None
    conn.execute(
        sql.SQL("UPDATE {} SET last_seen_at = %s WHERE session_id = %s").format(
            _t(schema, "sessions")
        ),
        (max(now, last_seen), session_id),
    )
    return Session(session_id, account_id, username, csrf, created, expires)


def end_session(conn: psycopg.Connection, schema: str, session_id: int) -> None:
    conn.execute(
        sql.SQL("DELETE FROM {} WHERE session_id = %s").format(_t(schema, "sessions")),
        (session_id,),
    )


def owner_status(conn: psycopg.Connection, schema: str, now: datetime) -> dict:
    """Facts for `jobmarket owner status` (never the password or tokens)."""
    account = conn.execute(
        sql.SQL("SELECT username, created_at, password_changed_at FROM {}").format(
            _t(schema, "accounts")
        )
    ).fetchone()
    sessions = conn.execute(
        sql.SQL("SELECT count(*) FROM {} WHERE expires_at > %s").format(_t(schema, "sessions")),
        (now,),
    ).fetchone()[0]
    return {
        "exists": account is not None,
        "username": account[0] if account else None,
        "created_at": account[1] if account else None,
        "password_changed_at": account[2] if account else None,
        "open_sessions": sessions,
        "paused_until": paused_until(conn, schema, now),
    }
