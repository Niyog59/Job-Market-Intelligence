"""The owner account (jobmarket.owner): provisioning, passwords, throttling, sessions, and the
private schema created by the migrations. Fictional credentials only."""

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("argon2", reason="owner accounts need argon2-cffi (pip install -e '.[web]')")

from jobmarket import owner  # noqa: E402
from jobmarket.migrate import apply_migrations, current_private_schema  # noqa: E402

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
PASSWORD = "correct horse battery"  # fictional


@pytest.fixture
def private(schema_conn, isolated_schema):
    apply_migrations(schema_conn)
    schema = current_private_schema(schema_conn)
    assert schema == f"{isolated_schema}_private"

    class P:
        conn, name = schema_conn, schema

        def scalar(self, query, params=()):
            return self.conn.execute(query.format(p=f'"{schema}"'), params).fetchone()[0]

    return P()


def test_private_tables_live_outside_the_ingestion_schema(private, isolated_schema):
    tables = dict(private.conn.execute(
        "SELECT tablename, schemaname FROM pg_tables WHERE tablename IN "
        "('accounts', 'sessions', 'login_attempts')").fetchall())  # fmt: skip
    assert set(tables.values()) == {private.name}
    assert isolated_schema not in tables.values()


def test_the_password_is_stored_only_as_an_argon2id_hash(private):
    owner.create_owner(private.conn, private.name, "owner", PASSWORD, NOW)
    stored = private.scalar("SELECT password_hash FROM {p}.accounts")
    assert stored.startswith("$argon2id$") and PASSWORD not in stored


def test_only_one_owner_and_rules_for_names_and_passwords(private):
    with pytest.raises(owner.OwnerError, match="at least 8"):
        owner.create_owner(private.conn, private.name, "owner", "short", NOW)
    with pytest.raises(owner.OwnerError, match="username"):
        owner.create_owner(private.conn, private.name, "a b", PASSWORD, NOW)
    owner.create_owner(private.conn, private.name, "owner", PASSWORD, NOW)
    with pytest.raises(owner.OwnerError, match="already exists"):
        owner.create_owner(private.conn, private.name, "second", PASSWORD, NOW)


def test_sign_in_stores_only_a_hash_of_the_browser_token(private):
    owner.create_owner(private.conn, private.name, "owner", PASSWORD, NOW)
    token, session = owner.sign_in(private.conn, private.name, "owner", PASSWORD, NOW)
    assert len(token) >= 40
    assert private.scalar("SELECT token_hash FROM {p}.sessions") == owner.token_hash(token)
    assert private.scalar("SELECT count(*) FROM {p}.sessions WHERE token_hash = %s",
                          (token,)) == 0  # fmt: skip
    assert owner.load_session(private.conn, private.name, token, NOW).session_id == (
        session.session_id
    )
    assert owner.sign_in(private.conn, private.name, "owner", "wrong password", NOW) is None
    assert owner.sign_in(private.conn, private.name, "someone", PASSWORD, NOW) is None


def test_every_three_wrong_passwords_pause_sign_in_for_15_minutes(private):
    owner.create_owner(private.conn, private.name, "owner", PASSWORD, NOW)

    def attempt(at, password="wrong"):
        return owner.sign_in(private.conn, private.name, "owner", password, at)

    for i in range(3):
        assert attempt(NOW + timedelta(seconds=i)) is None
    with pytest.raises(owner.SignInPaused) as paused:
        attempt(NOW + timedelta(minutes=5), PASSWORD)  # even the right password waits
    assert paused.value.until == NOW + timedelta(seconds=2) + owner.PAUSE
    after = NOW + timedelta(minutes=16)
    # After the pause: three more tries, then another pause.
    for i in range(3):
        assert attempt(after + timedelta(seconds=i)) is None
    with pytest.raises(owner.SignInPaused):
        attempt(after + timedelta(minutes=1), PASSWORD)
    later = after + timedelta(minutes=20)
    assert attempt(later, PASSWORD) is not None  # success resets the count
    assert attempt(later) is None and attempt(later) is None
    assert attempt(later, PASSWORD) is not None  # two failures do not pause


def test_sessions_end_after_2_hours_idle_or_7_days(private):
    owner.create_owner(private.conn, private.name, "owner", PASSWORD, NOW)
    token, _ = owner.sign_in(private.conn, private.name, "owner", PASSWORD, NOW)
    assert owner.load_session(
        private.conn, private.name, token, NOW + timedelta(hours=1, minutes=59)
    )
    assert owner.load_session(private.conn, private.name, token, NOW + timedelta(hours=4)) is None
    assert private.scalar("SELECT count(*) FROM {p}.sessions") == 0  # deleted when expired

    token, _ = owner.sign_in(private.conn, private.name, "owner", PASSWORD, NOW)
    at = NOW
    while at < NOW + owner.ABSOLUTE_TIMEOUT - timedelta(hours=1):
        at += timedelta(hours=1)
        assert owner.load_session(private.conn, private.name, token, at)
    assert owner.load_session(private.conn, private.name, token,
                              NOW + owner.ABSOLUTE_TIMEOUT) is None  # fmt: skip


def test_unknown_or_oversized_tokens_are_not_sessions(private):
    assert owner.load_session(private.conn, private.name, "made-up-token", NOW) is None
    assert owner.load_session(private.conn, private.name, "x" * 5000, NOW) is None
    assert owner.load_session(private.conn, private.name, None, NOW) is None


def test_password_reset_ends_sessions_and_clears_the_pause(private):
    owner.create_owner(private.conn, private.name, "owner", PASSWORD, NOW)
    token, _ = owner.sign_in(private.conn, private.name, "owner", PASSWORD, NOW)
    for _ in range(3):
        owner.sign_in(private.conn, private.name, "owner", "wrong", NOW)
    assert owner.paused_until(private.conn, private.name, NOW) is not None
    assert owner.reset_password(private.conn, private.name, "a new fictional pass", NOW) == 1
    assert owner.paused_until(private.conn, private.name, NOW) is None
    assert owner.load_session(private.conn, private.name, token, NOW) is None
    assert owner.sign_in(private.conn, private.name, "owner", PASSWORD, NOW) is None
    assert owner.sign_in(private.conn, private.name, "owner", "a new fictional pass", NOW)


def test_status_never_includes_secrets(private):
    owner.create_owner(private.conn, private.name, "owner", PASSWORD, NOW)
    owner.sign_in(private.conn, private.name, "owner", PASSWORD, NOW)
    info = owner.owner_status(private.conn, private.name, NOW)
    assert info["username"] == "owner" and info["open_sessions"] == 1
    assert PASSWORD not in repr(info) and "argon2" not in repr(info)


def test_changing_the_password_needs_the_current_one_and_ends_other_sessions(private):
    owner.create_owner(private.conn, private.name, "owner", PASSWORD, NOW)
    other, _ = owner.sign_in(private.conn, private.name, "owner", PASSWORD, NOW)
    with pytest.raises(owner.WrongPassword):
        owner.change_password(private.conn, private.name, "not it", "a fresh fictional pass", NOW)
    with pytest.raises(owner.OwnerError, match="at least 8"):
        owner.change_password(private.conn, private.name, PASSWORD, "short", NOW)
    token, _ = owner.change_password(private.conn, private.name, PASSWORD,
                                     "a fresh fictional pass", NOW)  # fmt: skip
    assert owner.load_session(private.conn, private.name, other, NOW) is None
    assert owner.load_session(private.conn, private.name, token, NOW) is not None
    assert owner.sign_in(private.conn, private.name, "owner", "a fresh fictional pass", NOW)


def test_wrong_current_passwords_count_toward_the_pause(private):
    owner.create_owner(private.conn, private.name, "owner", PASSWORD, NOW)
    for _ in range(3):
        with pytest.raises(owner.WrongPassword):
            owner.change_password(private.conn, private.name, "guess", "new fictional pass", NOW)
    with pytest.raises(owner.SignInPaused):
        owner.change_password(private.conn, private.name, PASSWORD, "new fictional pass", NOW)
