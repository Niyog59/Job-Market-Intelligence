-- Core collection schema: employers, their public job boards, scan runs, preserved raw
-- responses, and the identity, content versions, and observations of each posting.
--
-- Conventions:
--   * Surrogate keys are identity columns; natural identities are UNIQUE constraints.
--   * All timestamps are timestamptz.
--   * Status values are text with CHECK constraints (easier to extend than enum types).
--   * Foreign keys use the default NO ACTION, so a parent row with history cannot be
--     deleted by accident.
--   * raw_responses, posting_versions, and posting_observations are append-only by
--     convention: rows are inserted, never updated or deleted.

CREATE TABLE employers (
    employer_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         text NOT NULL CHECK (btrim(name) <> ''),
    slug         text NOT NULL CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    website_url  text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT employers_slug_key UNIQUE (slug)
);

-- One public job board for one employer in one source system. An employer can have
-- several boards.
CREATE TABLE source_boards (
    board_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    employer_id    bigint NOT NULL REFERENCES employers (employer_id),
    source_system  text NOT NULL
                   CHECK (source_system IN ('greenhouse', 'lever', 'ashby', 'careers_page')),
    board_key      text NOT NULL CHECK (btrim(board_key) <> ''),
    board_url      text NOT NULL,
    is_active      boolean NOT NULL DEFAULT true,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT source_boards_identity_key UNIQUE (source_system, board_key)
);

CREATE INDEX source_boards_employer_id_idx ON source_boards (employer_id);

COMMENT ON COLUMN source_boards.is_active IS
    'false means the board is no longer scanned; its history is kept.';

-- One scheduled or manual scan across boards.
CREATE TABLE collection_runs (
    run_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trigger      text NOT NULL CHECK (trigger IN ('scheduled', 'manual')),
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    status       text NOT NULL CHECK (status IN ('running', 'complete', 'partial', 'failed')),
    CONSTRAINT collection_runs_finished_after_started CHECK (finished_at >= started_at),
    CONSTRAINT collection_runs_finished_iff_not_running
        CHECK ((status = 'running') = (finished_at IS NULL))
);

COMMENT ON COLUMN collection_runs.status IS
    'complete: every board scan complete. failed: no board scan produced usable data. '
    'partial: anything in between. running: in progress, or the process died.';

-- The result of scanning one board during one run.
CREATE TABLE board_scans (
    board_scan_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id         bigint NOT NULL REFERENCES collection_runs (run_id),
    board_id       bigint NOT NULL REFERENCES source_boards (board_id),
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    status         text NOT NULL CHECK (status IN ('running', 'complete', 'partial', 'failed')),
    postings_seen  integer CHECK (postings_seen >= 0),
    error_message  text,
    CONSTRAINT board_scans_run_board_key UNIQUE (run_id, board_id),
    CONSTRAINT board_scans_finished_after_started CHECK (finished_at >= started_at),
    CONSTRAINT board_scans_finished_iff_not_running
        CHECK ((status = 'running') = (finished_at IS NULL)),
    -- A complete scan always knows how many postings it saw (possibly 0).
    CONSTRAINT board_scans_complete_has_count
        CHECK (status <> 'complete' OR postings_seen IS NOT NULL)
);

CREATE INDEX board_scans_board_id_started_at_idx ON board_scans (board_id, started_at);

COMMENT ON COLUMN board_scans.status IS
    'Only a complete scan may be used as evidence that a posting is missing. '
    'partial and failed scans never imply that a job closed.';
COMMENT ON COLUMN board_scans.postings_seen IS
    'NULL means unknown (for example a failed scan). 0 means the board listed no postings.';

-- A preserved raw response that was actually received. The bytes are stored outside the
-- database at storage_uri; requests that got no response are recorded on board_scans.
CREATE TABLE raw_responses (
    raw_response_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    board_scan_id    bigint NOT NULL REFERENCES board_scans (board_scan_id),
    sequence_no      integer NOT NULL CHECK (sequence_no >= 1),
    request_url      text NOT NULL,
    http_status      integer NOT NULL CHECK (http_status BETWEEN 100 AND 599),
    fetched_at       timestamptz NOT NULL,
    storage_uri      text NOT NULL,
    content_sha256   text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    byte_size        bigint NOT NULL CHECK (byte_size >= 0),
    CONSTRAINT raw_responses_storage_uri_key UNIQUE (storage_uri),
    CONSTRAINT raw_responses_scan_sequence_key UNIQUE (board_scan_id, sequence_no),
    -- Target for the composite foreign key on posting_observations.
    CONSTRAINT raw_responses_id_scan_key UNIQUE (raw_response_id, board_scan_id)
);

-- The identity of one job as one board knows it.
CREATE TABLE source_postings (
    posting_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    board_id             bigint NOT NULL REFERENCES source_boards (board_id),
    source_posting_id    text NOT NULL CHECK (btrim(source_posting_id) <> ''),
    first_discovered_at  timestamptz NOT NULL,
    last_observed_at     timestamptz NOT NULL,
    CONSTRAINT source_postings_identity_key UNIQUE (board_id, source_posting_id),
    CONSTRAINT source_postings_observed_after_discovered
        CHECK (last_observed_at >= first_discovered_at)
);

CREATE INDEX source_postings_first_discovered_at_idx
    ON source_postings (first_discovered_at DESC);

COMMENT ON COLUMN source_postings.first_discovered_at IS
    'When this system first saw the posting. Set once, never updated.';
COMMENT ON COLUMN source_postings.last_observed_at IS
    'When this system last actually saw the posting. Summary of posting_observations, '
    'which are authoritative.';

-- Each distinct content of a posting. A new version is written only when the content
-- hash differs from the latest version; returning to earlier content (A, B, A) is a new
-- version, so content_hash is deliberately not unique per posting.
CREATE TABLE posting_versions (
    version_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    posting_id        bigint NOT NULL REFERENCES source_postings (posting_id),
    version_no        integer NOT NULL CHECK (version_no >= 1),
    content_hash      text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    title             text NOT NULL,
    location_text     text,
    description_text  text,
    first_seen_at     timestamptz NOT NULL,
    CONSTRAINT posting_versions_posting_version_key UNIQUE (posting_id, version_no),
    -- Target for the composite foreign key on posting_observations.
    CONSTRAINT posting_versions_id_posting_key UNIQUE (version_id, posting_id)
);

COMMENT ON COLUMN posting_versions.first_seen_at IS
    'When this content was first observed. For version_no >= 2 this is the content change time.';

-- "During board scan S we saw posting P with content version V, in raw response R."
CREATE TABLE posting_observations (
    observation_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    board_scan_id        bigint NOT NULL REFERENCES board_scans (board_scan_id),
    posting_id           bigint NOT NULL REFERENCES source_postings (posting_id),
    version_id           bigint NOT NULL,
    raw_response_id      bigint NOT NULL,
    observed_at          timestamptz NOT NULL,
    source_published_at  timestamptz,
    source_updated_at    timestamptz,
    CONSTRAINT posting_observations_scan_posting_key UNIQUE (board_scan_id, posting_id),
    -- The version must belong to the same posting.
    CONSTRAINT posting_observations_version_fkey
        FOREIGN KEY (version_id, posting_id)
        REFERENCES posting_versions (version_id, posting_id),
    -- The raw response must come from the same board scan.
    CONSTRAINT posting_observations_raw_response_fkey
        FOREIGN KEY (raw_response_id, board_scan_id)
        REFERENCES raw_responses (raw_response_id, board_scan_id)
);

CREATE INDEX posting_observations_posting_id_observed_at_idx
    ON posting_observations (posting_id, observed_at);

COMMENT ON COLUMN posting_observations.source_published_at IS
    'Publication time as the source stated it in this scan. NULL means the source did not say.';
COMMENT ON COLUMN posting_observations.source_updated_at IS
    'The source''s own last-updated claim in this scan, if any.';
