-- The private workspace of the single owner account: sign-in data first (profile, saved jobs,
-- and applications follow in later migrations).
--
-- Everything private lives in its own schema (__PRIVATE__ is replaced by the migration
-- runner: "private" next to "public"). The read-only analytics login is granted the
-- ingestion schema only, so dbt, the serving views, and every public page cannot read these
-- tables. The website reaches them through a separate login (jobmarket_private) granted this
-- schema only. Migrations create structure; they never contain private records.

CREATE SCHEMA __PRIVATE__;
REVOKE ALL ON SCHEMA __PRIVATE__ FROM PUBLIC;

COMMENT ON SCHEMA __PRIVATE__ IS
    'Private owner data (sign-in, profile, saved jobs, applications). Never granted to the '
    'analytics login; excluded from analytics, exports, and public pages.';

-- One owner account only: there is no registration. It is created and recovered from the
-- command line by someone with owner access to the database (jobmarket owner create /
-- reset-password).
CREATE TABLE __PRIVATE__.accounts (
    account_id          smallint PRIMARY KEY DEFAULT 1 CHECK (account_id = 1),
    username            text NOT NULL CHECK (username ~ '^[A-Za-z0-9._-]{3,40}$'),
    password_hash       text NOT NULL CHECK (password_hash LIKE '$argon2id$%'),
    created_at          timestamptz NOT NULL,
    password_changed_at timestamptz NOT NULL
);

-- Server-side sessions. The browser holds a random token; only its SHA-256 is stored, so a
-- copy of this table cannot be used to sign in. Signing out deletes the row.
CREATE TABLE __PRIVATE__.sessions (
    session_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id   smallint NOT NULL REFERENCES __PRIVATE__.accounts (account_id)
                     ON DELETE CASCADE,
    token_hash   text NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    csrf_token   text NOT NULL CHECK (length(csrf_token) >= 32),
    created_at   timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    expires_at   timestamptz NOT NULL,
    CHECK (last_seen_at >= created_at AND expires_at > created_at)
);

-- Sign-in attempts, for throttling (consecutive failures pause sign-in). The submitted
-- username and password are never stored.
CREATE TABLE __PRIVATE__.login_attempts (
    attempt_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    attempted_at timestamptz NOT NULL,
    succeeded    boolean NOT NULL,
    remote_addr  text
);

CREATE INDEX login_attempts_time_idx ON __PRIVATE__.login_attempts (attempted_at DESC);
