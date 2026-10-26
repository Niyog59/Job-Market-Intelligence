-- Greenhouse collection: source registry details, per-scan counts, relevance screening,
-- rejected records, retry incidents, an alert outbox, duplicate flags, and raw-response
-- retention.

-- Retries are a third kind of run.
ALTER TABLE collection_runs DROP CONSTRAINT collection_runs_trigger_check;
ALTER TABLE collection_runs ADD CONSTRAINT collection_runs_trigger_check
    CHECK (trigger IN ('scheduled', 'manual', 'retry'));

-- Registry details loaded from config/greenhouse_boards.toml.
ALTER TABLE employers ADD COLUMN corporate_group text;

ALTER TABLE source_boards
    ADD COLUMN hosting_region text NOT NULL DEFAULT 'us'
        CHECK (hosting_region IN ('us', 'eu')),
    ADD COLUMN verification_status text NOT NULL DEFAULT 'pending'
        CHECK (verification_status IN ('verified', 'pending')),
    ADD COLUMN metros text[] NOT NULL DEFAULT '{}',
    ADD COLUMN local_evidence text,
    ADD COLUMN official_source_url text,
    ADD COLUMN evidence_notes text,
    ADD COLUMN special_checks text,
    ADD COLUMN registry_synced_at timestamptz;

-- Boards created before this migration have no verification yet; `jobmarket sources sync`
-- re-enables verified ones from the registry.
UPDATE source_boards SET is_active = false WHERE verification_status <> 'verified';

-- Only verified boards may be collected, so new boards start disabled.
ALTER TABLE source_boards ALTER COLUMN is_active SET DEFAULT false;
ALTER TABLE source_boards ADD CONSTRAINT source_boards_active_requires_verified
    CHECK (NOT is_active OR verification_status = 'verified');

-- What each board scan received and kept. NULL means unknown (for example a failed scan).
ALTER TABLE board_scans
    ADD COLUMN attempt_kind text NOT NULL DEFAULT 'initial'
        CHECK (attempt_kind IN ('initial', 'retry', 'extra')),
    ADD COLUMN jobs_reported integer CHECK (jobs_reported >= 0),
    ADD COLUMN jobs_received integer CHECK (jobs_received >= 0),
    ADD COLUMN jobs_invalid integer CHECK (jobs_invalid >= 0),
    ADD COLUMN jobs_retained integer CHECK (jobs_retained >= 0),
    ADD COLUMN jobs_excluded integer CHECK (jobs_excluded >= 0),
    ADD COLUMN jobs_needing_review integer CHECK (jobs_needing_review >= 0);

COMMENT ON COLUMN board_scans.attempt_kind IS
    'initial: first attempt of a run. retry: an attempt that consumed one of the three '
    'retries of an open incident. extra: an attempt made while an incident was open but '
    'before its next retry was due (does not use or reset the retry budget).';

-- Raw bytes are deleted after the retention period; the row (URL, status, fingerprint,
-- size) is kept so history and foreign keys stay intact.
ALTER TABLE raw_responses
    ADD COLUMN response_headers jsonb,
    ADD COLUMN purged_at timestamptz;

COMMENT ON COLUMN raw_responses.purged_at IS
    'When the stored bytes were deleted by retention cleanup. The row itself is kept.';

-- Evidence for future lifecycle decisions (Module 6). Only complete scans count.
ALTER TABLE source_postings
    ADD COLUMN complete_scan_absences integer NOT NULL DEFAULT 0
        CHECK (complete_scan_absences >= 0),
    ADD COLUMN last_absent_scan_id bigint REFERENCES board_scans (board_scan_id);

COMMENT ON COLUMN source_postings.complete_scan_absences IS
    'Consecutive COMPLETE board scans in which this posting was missing. Failed and '
    'partial scans never change it. Nothing is closed in Module 3.';

-- Full source content for retained postings.
ALTER TABLE posting_versions
    ADD COLUMN absolute_url text,
    ADD COLUMN content_html text,
    ADD COLUMN departments jsonb,
    ADD COLUMN offices jsonb,
    ADD COLUMN requisition_id text,
    ADD COLUMN internal_job_id bigint,
    ADD COLUMN source_metadata jsonb,
    ADD COLUMN screening jsonb;

COMMENT ON COLUMN posting_versions.absolute_url IS 'Application URL exactly as returned by the source.';
COMMENT ON COLUMN posting_versions.screening IS 'Screening result when this version was recorded.';

-- Current screening decision for every valid posting seen, retained or not.
CREATE TABLE posting_screenings (
    posting_id          bigint PRIMARY KEY REFERENCES source_postings (posting_id),
    content_hash        text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    classifier_version  text NOT NULL,
    title               text NOT NULL,
    location_text       text,
    retained            boolean NOT NULL,
    relevant            boolean NOT NULL,
    families            text[] NOT NULL DEFAULT '{}',
    texas_eligibility   text NOT NULL CHECK (texas_eligibility IN (
                            'local_dfw', 'local_houston', 'local_both', 'remote_texas',
                            'remote_ambiguous', 'not_texas', 'unknown')),
    city_targeted_remote boolean NOT NULL DEFAULT false,
    seniority           text NOT NULL CHECK (seniority IN (
                            'intern', 'entry', 'mid', 'senior', 'lead', 'manager', 'unknown')),
    min_years_required  integer CHECK (min_years_required >= 0),
    early_career        boolean NOT NULL,
    sponsorship_status  text NOT NULL CHECK (sponsorship_status IN (
                            'not_stated', 'available', 'not_available', 'asked_only')),
    sponsorship_evidence text,
    needs_review        boolean NOT NULL DEFAULT false,
    reasons             jsonb NOT NULL DEFAULT '[]',
    screened_at         timestamptz NOT NULL,
    CONSTRAINT posting_screenings_retained_requires_relevant CHECK (NOT retained OR relevant)
);

CREATE INDEX posting_screenings_retained_idx ON posting_screenings (retained) WHERE retained;

COMMENT ON COLUMN posting_screenings.sponsorship_status IS
    'not_stated unless the text says sponsorship is or is not available. asked_only: the '
    'text only asks about sponsorship, which does not prove availability.';

-- Records from a usable response that failed validation.
CREATE TABLE rejected_records (
    rejected_record_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    board_scan_id       bigint NOT NULL REFERENCES board_scans (board_scan_id),
    raw_response_id     bigint REFERENCES raw_responses (raw_response_id),
    record_index        integer,
    source_posting_id   text,
    reason              text NOT NULL,
    detail              jsonb,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX rejected_records_board_scan_id_idx ON rejected_records (board_scan_id);

-- Alerts and recovery notices waiting to be delivered.
CREATE TABLE notifications (
    notification_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind             text NOT NULL CHECK (kind IN ('alert', 'recovery')),
    subject          text NOT NULL,
    body             text NOT NULL,
    created_at       timestamptz NOT NULL,
    status           text NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'sent', 'failed')),
    attempts         integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error       text,
    sent_at          timestamptz,
    CONSTRAINT notifications_sent_has_time CHECK ((status = 'sent') = (sent_at IS NOT NULL))
);

-- One incident per run of consecutive unsuccessful scans of a board.
CREATE TABLE collection_incidents (
    incident_id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    board_id                 bigint NOT NULL REFERENCES source_boards (board_id),
    opened_at                timestamptz NOT NULL,
    opened_by_scan_id        bigint NOT NULL REFERENCES board_scans (board_scan_id),
    kind                     text NOT NULL CHECK (kind IN ('failed', 'partial')),
    status                   text NOT NULL CHECK (status IN ('retrying', 'exhausted', 'resolved')),
    retries_used             integer NOT NULL DEFAULT 0 CHECK (retries_used BETWEEN 0 AND 3),
    next_retry_at            timestamptz,
    last_scan_id             bigint NOT NULL REFERENCES board_scans (board_scan_id),
    last_error               text,
    alert_notification_id    bigint REFERENCES notifications (notification_id),
    resolved_at              timestamptz,
    resolved_by_scan_id      bigint REFERENCES board_scans (board_scan_id),
    recovery_notification_id bigint REFERENCES notifications (notification_id),
    CONSTRAINT collection_incidents_retrying_has_next
        CHECK ((status = 'retrying') = (next_retry_at IS NOT NULL)),
    CONSTRAINT collection_incidents_resolved_has_time
        CHECK ((status = 'resolved') = (resolved_at IS NOT NULL)),
    CONSTRAINT collection_incidents_recovery_needs_alert
        CHECK (recovery_notification_id IS NULL OR alert_notification_id IS NOT NULL)
);

-- At most one open incident per board.
CREATE UNIQUE INDEX collection_incidents_one_open_per_board
    ON collection_incidents (board_id) WHERE status <> 'resolved';

-- Possible duplicates are flagged for review, never merged.
CREATE TABLE posting_duplicate_flags (
    posting_id          bigint NOT NULL REFERENCES source_postings (posting_id),
    related_posting_id  bigint NOT NULL REFERENCES source_postings (posting_id),
    reason              text NOT NULL,
    detected_at         timestamptz NOT NULL,
    PRIMARY KEY (posting_id, related_posting_id),
    CHECK (posting_id < related_posting_id)
);

-- Latest scan per board.
CREATE VIEW latest_board_scans AS
SELECT DISTINCT ON (board_id) *
FROM board_scans
ORDER BY board_id, started_at DESC, board_scan_id DESC;

-- One row per board: what the status command and later the dashboard show.
CREATE VIEW board_status AS
SELECT
    b.board_id,
    e.name AS employer,
    b.board_key AS token,
    b.is_active AS enabled,
    b.verification_status,
    ls.status AS last_scan_status,
    ls.finished_at AS last_scan_finished_at,
    ls.error_message AS last_error,
    (SELECT max(s.finished_at) FROM board_scans s
      WHERE s.board_id = b.board_id AND s.status = 'complete') AS last_complete_at,
    (SELECT max(s.finished_at) FROM board_scans s
      WHERE s.board_id = b.board_id AND s.status IN ('complete', 'partial')) AS last_usable_at,
    i.incident_id,
    i.status AS incident_status,
    i.retries_used,
    i.next_retry_at,
    i.alert_notification_id IS NOT NULL AS alerted,
    (SELECT count(*) FROM source_postings p JOIN posting_screenings ps USING (posting_id)
      WHERE p.board_id = b.board_id AND ps.retained) AS retained_postings
FROM source_boards b
JOIN employers e USING (employer_id)
LEFT JOIN latest_board_scans ls ON ls.board_id = b.board_id
LEFT JOIN collection_incidents i ON i.board_id = b.board_id AND i.status <> 'resolved';

-- Retained jobs with their latest content, for listing and later the job search interface.
CREATE VIEW retained_jobs AS
SELECT
    p.posting_id,
    e.name AS employer,
    b.board_key AS token,
    p.source_posting_id,
    v.title,
    v.location_text,
    v.absolute_url,
    ps.families,
    ps.texas_eligibility,
    ps.city_targeted_remote,
    ps.seniority,
    ps.min_years_required,
    ps.early_career,
    ps.sponsorship_status,
    ps.needs_review,
    p.first_discovered_at,
    p.last_observed_at,
    v.version_no,
    v.first_seen_at AS content_seen_at,
    (SELECT o.source_published_at FROM posting_observations o
      WHERE o.posting_id = p.posting_id ORDER BY o.observed_at DESC LIMIT 1) AS source_published_at,
    p.complete_scan_absences
FROM source_postings p
JOIN posting_screenings ps USING (posting_id)
JOIN source_boards b USING (board_id)
JOIN employers e USING (employer_id)
JOIN LATERAL (
    SELECT * FROM posting_versions pv WHERE pv.posting_id = p.posting_id
    ORDER BY pv.version_no DESC LIMIT 1
) v ON true
WHERE ps.retained;
