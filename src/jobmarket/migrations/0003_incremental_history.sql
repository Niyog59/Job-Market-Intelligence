-- Incremental history: meaningful-change fingerprints under versioned normalization rules,
-- order-aware current state, processing time kept apart from collection time, and replay
-- records. Existing rows are backfilled; nothing is deleted.
--
-- Chronology everywhere is collection time (posting_observations.observed_at, the fetch
-- time of the response), with board_scan_id breaking exact ties. Processing time never
-- decides what is current.

-- Versions --------------------------------------------------------------------------------

ALTER TABLE posting_versions
    ADD COLUMN normalization_version text,
    ADD COLUMN detection text,
    ADD COLUMN recorded_at timestamptz;

-- Rows written before this migration were created by comparing exact content.
UPDATE posting_versions
SET detection = CASE WHEN version_no = 1 THEN 'first_seen' ELSE 'legacy_exact' END,
    recorded_at = first_seen_at;

ALTER TABLE posting_versions
    ALTER COLUMN detection SET NOT NULL,
    ALTER COLUMN recorded_at SET NOT NULL,
    ADD CONSTRAINT posting_versions_detection_check
        CHECK (detection IN ('first_seen', 'content_change', 'legacy_exact')),
    ADD CONSTRAINT posting_versions_change_has_rules
        CHECK (detection <> 'content_change' OR normalization_version IS NOT NULL);

COMMENT ON TABLE posting_versions IS
    'Each meaningfully distinct content stage of a posting, with its original source '
    'content. Rows are never updated. Returning to earlier content (A, B, A) is a new row. '
    'version_no is recording order; chronological order is in posting_version_stages.';
COMMENT ON COLUMN posting_versions.content_hash IS
    'Exact source fingerprint of the original content (every field except source '
    'timestamps). The meaningful-change test is posting_version_comparisons.fingerprint.';
COMMENT ON COLUMN posting_versions.first_seen_at IS
    'Collection time of the observation that created this row. The change itself happened '
    'after the previous stage was last observed; see posting_version_stages.';
COMMENT ON COLUMN posting_versions.normalization_version IS
    'Rules (jobmarket.normalize) under which this version was found to differ. NULL for '
    'rows recorded before comparison rules existed.';
COMMENT ON COLUMN posting_versions.detection IS
    'first_seen: first content kept for the posting. content_change: comparison form '
    'differed from the neighbouring observation. legacy_exact: recorded before comparison '
    'rules existed, when any exact-content difference created a version.';
COMMENT ON COLUMN posting_versions.recorded_at IS 'Processing time when the row was written.';

-- The comparison form of each version under each rule set it has been evaluated with. The
-- original content in posting_versions makes every row reproducible.
CREATE TABLE posting_version_comparisons (
    version_id             bigint NOT NULL REFERENCES posting_versions (version_id),
    normalization_version  text NOT NULL CHECK (btrim(normalization_version) <> ''),
    fingerprint            text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    normalized             jsonb NOT NULL,
    computed_at            timestamptz NOT NULL,
    PRIMARY KEY (version_id, normalization_version)
);

COMMENT ON TABLE posting_version_comparisons IS
    'If one version has different fingerprints under two rule sets, that difference comes '
    'from the rules, not from the employer: the original content is the same.';

-- Observations ----------------------------------------------------------------------------

ALTER TABLE posting_observations
    ADD COLUMN source_fingerprint text,
    ADD COLUMN normalization_version text,
    ADD COLUMN comparison_fingerprint text,
    ADD COLUMN processed_at timestamptz,
    ADD COLUMN recorded_by text;

-- Before this migration a new version was written whenever exact content differed from the
-- latest version, so every existing observation's content is exactly its version's content.
UPDATE posting_observations o
SET source_fingerprint = v.content_hash,
    processed_at = o.observed_at,
    recorded_by = 'collection'
FROM posting_versions v
WHERE v.version_id = o.version_id;

ALTER TABLE posting_observations
    ALTER COLUMN source_fingerprint SET NOT NULL,
    ALTER COLUMN processed_at SET NOT NULL,
    ALTER COLUMN recorded_by SET NOT NULL,
    ADD CONSTRAINT posting_observations_source_fingerprint_check
        CHECK (source_fingerprint ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT posting_observations_comparison_pair
        CHECK ((normalization_version IS NULL) = (comparison_fingerprint IS NULL)),
    ADD CONSTRAINT posting_observations_recorded_by_check
        CHECK (recorded_by IN ('collection', 'replay')),
    -- Target for the current-state foreign key on source_postings.
    ADD CONSTRAINT posting_observations_current_key UNIQUE (observation_id, posting_id, version_id);

DROP INDEX posting_observations_posting_id_observed_at_idx;
CREATE INDEX posting_observations_chronology_idx
    ON posting_observations (posting_id, observed_at, board_scan_id);

COMMENT ON COLUMN posting_observations.observed_at IS
    'Collection time: when the response containing this observation was fetched. All '
    'chronology uses this, never processing time.';
COMMENT ON COLUMN posting_observations.source_fingerprint IS
    'Exact source fingerprint of the content seen in this observation.';
COMMENT ON COLUMN posting_observations.comparison_fingerprint IS
    'Comparison fingerprint of this observation''s own content under normalization_version. '
    'NULL for observations recorded before comparison rules existed.';
COMMENT ON COLUMN posting_observations.processed_at IS
    'When this row was written. A replay writes it later than the data was collected.';

-- Current state -----------------------------------------------------------------------------

ALTER TABLE source_postings
    ADD COLUMN current_observation_id bigint,
    ADD COLUMN current_version_id bigint;

UPDATE source_postings p
SET current_observation_id = newest.observation_id,
    current_version_id = newest.version_id
FROM (
    SELECT DISTINCT ON (posting_id) posting_id, observation_id, version_id
    FROM posting_observations
    ORDER BY posting_id, observed_at DESC, board_scan_id DESC
) newest
WHERE newest.posting_id = p.posting_id;

ALTER TABLE source_postings
    ADD CONSTRAINT source_postings_current_pair
        CHECK ((current_observation_id IS NULL) = (current_version_id IS NULL)),
    -- The current version must be the version of the current observation, and both must
    -- belong to this posting.
    ADD CONSTRAINT source_postings_current_fkey
        FOREIGN KEY (current_observation_id, posting_id, current_version_id)
        REFERENCES posting_observations (observation_id, posting_id, version_id);

COMMENT ON COLUMN source_postings.first_discovered_at IS
    'Earliest collection time at which this system saw the posting. Moves earlier only when '
    'an older saved response is processed later; never moves later.';
COMMENT ON COLUMN source_postings.current_observation_id IS
    'The newest observation by collection time. An older response processed later adds '
    'history but never replaces it.';

-- Screening follows the same rule: it describes the newest content seen.
ALTER TABLE posting_screenings
    ADD COLUMN content_observed_at timestamptz,
    ADD COLUMN content_scan_id bigint REFERENCES board_scans (board_scan_id);

-- Before this migration everything was processed in collection order, so the screening
-- describes the content seen at the posting's last observation.
UPDATE posting_screenings ps
SET content_observed_at = p.last_observed_at,
    content_scan_id = (
        SELECT r.board_scan_id
        FROM raw_responses r
        JOIN board_scans s USING (board_scan_id)
        WHERE s.board_id = p.board_id AND r.fetched_at = p.last_observed_at
        ORDER BY r.board_scan_id DESC
        LIMIT 1
    )
FROM source_postings p
WHERE p.posting_id = ps.posting_id;

ALTER TABLE posting_screenings ALTER COLUMN content_observed_at SET NOT NULL;

COMMENT ON COLUMN posting_screenings.content_observed_at IS
    'Collection time of the content this screening describes. Older content processed '
    'later does not replace it.';
COMMENT ON COLUMN posting_screenings.screened_at IS 'When the screening was computed.';

-- Per-scan change counts ------------------------------------------------------------------

ALTER TABLE board_scans
    ADD COLUMN jobs_new integer CHECK (jobs_new >= 0),
    ADD COLUMN jobs_changed integer CHECK (jobs_changed >= 0),
    ADD COLUMN jobs_unchanged integer CHECK (jobs_unchanged >= 0);

COMMENT ON COLUMN board_scans.jobs_new IS 'Postings first discovered in this scan.';
COMMENT ON COLUMN board_scans.jobs_changed IS
    'Tracked postings whose content changed meaningfully (a new version was recorded).';
COMMENT ON COLUMN board_scans.jobs_unchanged IS
    'Tracked postings observed again with the same meaningful content.';

-- Reprocessing the same response must not repeat its rejected records.
ALTER TABLE rejected_records
    ADD CONSTRAINT rejected_records_response_index_key UNIQUE (raw_response_id, record_index);

-- Replays ----------------------------------------------------------------------------------

CREATE TABLE replay_runs (
    replay_id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    board_scan_id            bigint NOT NULL REFERENCES board_scans (board_scan_id),
    raw_response_id          bigint REFERENCES raw_responses (raw_response_id),
    collected_at             timestamptz,
    started_at               timestamptz NOT NULL,
    finished_at              timestamptz NOT NULL,
    outcome                  text NOT NULL CHECK (outcome IN (
                                 'processed', 'expired', 'file_missing', 'checksum_mismatch',
                                 'no_response', 'unusable', 'failed')),
    validation_outcome       text CHECK (validation_outcome IN ('complete', 'partial', 'failed')),
    detail                   text,
    postings_new             integer CHECK (postings_new >= 0),
    versions_new             integer CHECK (versions_new >= 0),
    observations_new         integer CHECK (observations_new >= 0),
    observations_existing    integer CHECK (observations_existing >= 0),
    observations_historical  integer CHECK (observations_historical >= 0),
    CONSTRAINT replay_runs_finished_after_started CHECK (finished_at >= started_at),
    CONSTRAINT replay_runs_processed_has_counts
        CHECK (outcome <> 'processed' OR observations_new IS NOT NULL)
);

CREATE INDEX replay_runs_board_scan_id_idx ON replay_runs (board_scan_id);

COMMENT ON TABLE replay_runs IS
    'Each attempt to reprocess a saved raw response under its original board scan and '
    'collection time. expired: the bytes were deleted by raw retention.';
COMMENT ON COLUMN replay_runs.observations_existing IS
    'Observations that were already recorded from this response (nothing was added).';
COMMENT ON COLUMN replay_runs.observations_historical IS
    'New observations older than the posting''s current observation: kept as history, '
    'current state unchanged.';

-- Derived history ---------------------------------------------------------------------------

-- Fields whose comparison form differs between two versions, judged under the newest rule
-- set both have been evaluated with. NULL if they share none.
CREATE FUNCTION version_changed_fields(previous_version bigint, current_version bigint)
RETURNS text[]
LANGUAGE sql STABLE AS $$
    SELECT ARRAY(
        SELECT key
        FROM jsonb_object_keys(cur.normalized || prev.normalized) AS key
        WHERE cur.normalized -> key IS DISTINCT FROM prev.normalized -> key
        ORDER BY key)
    FROM posting_version_comparisons cur
    JOIN posting_version_comparisons prev
      ON prev.version_id = previous_version
     AND prev.normalization_version = cur.normalization_version
    WHERE cur.version_id = current_version
    ORDER BY cur.normalization_version DESC
    LIMIT 1
$$;

-- Content stages in collection order: consecutive observations of the same version form one
-- stage, so A -> B -> A is three stages. A change into a stage happened after
-- change_window_start (the previous stage's last observation) and no later than
-- change_window_end (this stage's first observation); the exact moment is unknown.
CREATE VIEW posting_version_stages AS
WITH ordered AS (
    SELECT o.posting_id, o.version_id, o.observed_at, o.board_scan_id,
           lag(o.version_id) OVER (PARTITION BY o.posting_id
                                   ORDER BY o.observed_at, o.board_scan_id) AS previous_version
    FROM posting_observations o
), numbered AS (
    SELECT *,
           count(*) FILTER (WHERE previous_version IS DISTINCT FROM version_id)
               OVER (PARTITION BY posting_id ORDER BY observed_at, board_scan_id) AS stage_no
    FROM ordered
), stages AS (
    SELECT posting_id, stage_no, version_id,
           min(observed_at) AS first_observed_at,
           max(observed_at) AS last_observed_at,
           count(*) AS observations
    FROM numbered
    GROUP BY posting_id, stage_no, version_id
), linked AS (
    SELECT s.*,
           lag(s.version_id) OVER w AS previous_version_id,
           lag(s.last_observed_at) OVER w AS previous_last_observed_at
    FROM stages s
    WINDOW w AS (PARTITION BY s.posting_id ORDER BY s.stage_no)
)
SELECT
    l.posting_id,
    l.stage_no,
    l.version_id,
    v.version_no,
    v.title,
    l.first_observed_at,
    l.last_observed_at,
    l.observations,
    l.previous_version_id,
    l.previous_last_observed_at AS change_window_start,
    CASE WHEN l.previous_version_id IS NOT NULL THEN l.first_observed_at END AS change_window_end,
    CASE WHEN l.previous_version_id IS NOT NULL
         THEN version_changed_fields(l.previous_version_id, l.version_id) END AS changed_fields
FROM linked l
JOIN posting_versions v ON v.version_id = l.version_id;

-- Integrity checks that constraints cannot express. Every query here should return no rows.
CREATE VIEW posting_history_problems AS
WITH newest AS (
    SELECT DISTINCT ON (posting_id) posting_id, observation_id, observed_at
    FROM posting_observations
    ORDER BY posting_id, observed_at DESC, board_scan_id DESC
), bounds AS (
    SELECT posting_id, min(observed_at) AS first_at, max(observed_at) AS last_at
    FROM posting_observations
    GROUP BY posting_id
)
SELECT p.posting_id, 'current observation is not the newest observation' AS problem
FROM source_postings p JOIN newest n USING (posting_id)
WHERE p.current_observation_id IS DISTINCT FROM n.observation_id
UNION ALL
SELECT p.posting_id, 'first_discovered_at is later than the earliest observation'
FROM source_postings p JOIN bounds b USING (posting_id)
WHERE p.first_discovered_at > b.first_at
UNION ALL
SELECT p.posting_id, 'last_observed_at is earlier than the latest observation'
FROM source_postings p JOIN bounds b USING (posting_id)
WHERE p.last_observed_at < b.last_at
UNION ALL
SELECT v.posting_id, 'version has no observations'
FROM posting_versions v
WHERE NOT EXISTS (SELECT 1 FROM posting_observations o WHERE o.version_id = v.version_id)
UNION ALL
SELECT ps.posting_id, 'screening describes content older than the current observation'
FROM posting_screenings ps JOIN newest n USING (posting_id)
WHERE ps.content_observed_at < n.observed_at;

-- Views that showed "the latest version" now follow the current-state pointer, so an older
-- response processed later cannot change what they show.
CREATE OR REPLACE VIEW retained_jobs AS
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
    co.source_published_at,
    p.complete_scan_absences
FROM source_postings p
JOIN posting_screenings ps USING (posting_id)
JOIN source_boards b USING (board_id)
JOIN employers e USING (employer_id)
JOIN posting_versions v ON v.version_id = p.current_version_id
JOIN posting_observations co ON co.observation_id = p.current_observation_id
WHERE ps.retained;
