-- Multi-source collection: Lever boards alongside Greenhouse boards, paginated scans,
-- and source fields that Greenhouse does not have.

-- Lever calls its default instance "global" (job pages on jobs.lever.co) and has an EU
-- instance; Greenhouse boards are "us" or "eu".
ALTER TABLE source_boards DROP CONSTRAINT source_boards_hosting_region_check;
ALTER TABLE source_boards ADD CONSTRAINT source_boards_hosting_region_check
    CHECK (hosting_region IN ('us', 'eu', 'global'));

-- One board can carry several hiring brands (for example a shared BoxLunch and Hot Topic
-- board); it is still scanned once.
ALTER TABLE source_boards ADD COLUMN hiring_brands text[] NOT NULL DEFAULT '{}';

-- Number of HTTP requests a scan made (Lever pages through a board; Greenhouse makes one).
-- Each received page is a raw_responses row with its sequence_no.
ALTER TABLE board_scans ADD COLUMN pages_fetched integer CHECK (pages_fetched >= 0);

COMMENT ON COLUMN board_scans.pages_fetched IS
    'Requests made for this scan, including any that got no response. A paginated scan is '
    'complete only when it reached the end of the board.';

-- Source fields that are not common to every source, kept per version for provenance.
ALTER TABLE posting_versions
    ADD COLUMN apply_url text,
    ADD COLUMN source_fields jsonb;

COMMENT ON COLUMN posting_versions.absolute_url IS
    'The posting page exactly as returned by the source (Greenhouse absolute_url, Lever '
    'hostedUrl).';
COMMENT ON COLUMN posting_versions.apply_url IS
    'A separate application page, when the source gives one (Lever applyUrl).';
COMMENT ON COLUMN posting_versions.source_fields IS
    'Structured fields under the source''s own names (for Lever: categories with '
    'allLocations, workplaceType, country, salaryRange, URLs, createdAt).';

-- Lever returns the time a posting was created (createdAt, not in its documentation). It
-- is not a publication date and is never used as one.
ALTER TABLE posting_observations ADD COLUMN source_created_at timestamptz;

COMMENT ON COLUMN posting_observations.source_created_at IS
    'When the source says the posting record was created (Lever createdAt). Not a '
    'publication date; NULL when the source does not say.';

-- Per-board status for the status command and later the dashboard. The latest attempt,
-- the latest complete scan, and the open incident are reported separately: a board can
-- have usable data from an earlier scan while its latest attempt failed.
DROP VIEW board_status;
-- latest_board_scans was created with SELECT *, which fixes its columns at creation time;
-- recreate it so it includes the columns added to board_scans since.
DROP VIEW latest_board_scans;
CREATE VIEW latest_board_scans AS
SELECT DISTINCT ON (board_id) *
FROM board_scans
ORDER BY board_id, started_at DESC, board_scan_id DESC;

CREATE VIEW board_status AS
SELECT
    b.board_id,
    e.name AS employer,
    b.source_system,
    b.board_key AS token,
    b.hosting_region,
    b.is_active AS enabled,
    b.verification_status,
    ls.status AS last_scan_status,
    ls.started_at AS last_attempt_at,
    ls.attempt_kind AS last_attempt_kind,
    ls.finished_at AS last_scan_finished_at,
    ls.pages_fetched AS last_scan_pages,
    ls.error_message AS last_error,
    (SELECT max(s.finished_at) FROM board_scans s
      WHERE s.board_id = b.board_id AND s.status = 'complete') AS last_complete_at,
    (SELECT max(s.finished_at) FROM board_scans s
      WHERE s.board_id = b.board_id AND s.status IN ('complete', 'partial')) AS last_usable_at,
    i.incident_id,
    i.status AS incident_status,
    i.kind AS incident_kind,
    i.opened_at AS incident_opened_at,
    i.retries_used,
    i.next_retry_at,
    coalesce(i.status = 'retrying' AND i.next_retry_at <= now(), false) AS retry_due,
    (SELECT count(*) FROM board_scans s
      WHERE s.board_id = b.board_id AND i.incident_id IS NOT NULL
        AND s.board_scan_id >= i.opened_by_scan_id) AS incident_attempts,
    i.alert_notification_id IS NOT NULL AS alerted,
    (SELECT count(*) FROM source_postings p JOIN posting_screenings ps USING (posting_id)
      WHERE p.board_id = b.board_id AND ps.retained) AS retained_postings
FROM source_boards b
JOIN employers e USING (employer_id)
LEFT JOIN latest_board_scans ls ON ls.board_id = b.board_id
LEFT JOIN collection_incidents i ON i.board_id = b.board_id AND i.status <> 'resolved';

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
    p.complete_scan_absences,
    b.source_system,
    v.apply_url,
    co.source_created_at
FROM source_postings p
JOIN posting_screenings ps USING (posting_id)
JOIN source_boards b USING (board_id)
JOIN employers e USING (employer_id)
JOIN posting_versions v ON v.version_id = p.current_version_id
JOIN posting_observations co ON co.observation_id = p.current_observation_id
WHERE ps.retained;
