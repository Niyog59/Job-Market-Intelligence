-- Content history for every listing (decision 2026-10-27). Until now only postings that
-- were, or had been, retained got observations and versions; the description of every other
-- listing lived only in raw responses, which are deleted after four days. From now on every
-- valid posting gets the same observations and versions, so later processing (requirement
-- extraction, re-screening) can read any listing.

ALTER TABLE posting_versions
    ADD COLUMN description_length integer CHECK (description_length >= 0),
    ADD COLUMN description_omitted text CHECK (description_omitted IN ('too_long'));

COMMENT ON COLUMN posting_versions.description_length IS
    'Characters of visible description text as collected, also when the text is omitted.';
COMMENT ON COLUMN posting_versions.description_omitted IS
    'too_long: the description exceeded the storage limit (20,000 characters), so its text '
    'and HTML are not stored. The fingerprint and comparison fingerprint are kept, so edits '
    'are still detected; readers get the posting link instead.';

ALTER TABLE posting_versions ADD CONSTRAINT posting_versions_omitted_has_no_text CHECK (
    description_omitted IS NULL OR (description_text IS NULL AND content_html IS NULL));

UPDATE posting_versions SET description_length = length(coalesce(description_text, ''));

-- Every listing with its current content and freshness, retained or not: the base the job
-- list and detail pages will read. Retained-only views (retained_jobs, active_jobs) are
-- unchanged.
CREATE VIEW listings AS
SELECT
    p.posting_id,
    e.name AS employer,
    b.source_system,
    b.board_key AS token,
    p.source_posting_id,
    p.identity_basis,
    v.version_id,
    v.version_no,
    v.title,
    v.location_text,
    v.absolute_url,
    v.apply_url,
    v.description_length,
    v.description_omitted,
    ps.retained,
    ps.needs_review,
    ps.families AS rule_families,
    ps.texas_eligibility AS rule_texas_eligibility,
    p.availability_state,
    p.qualifying_absences,
    p.availability_changed_at,
    p.first_discovered_at,
    p.last_observed_at,
    v.first_seen_at AS content_seen_at,
    co.source_published_at,
    co.source_created_at,
    co.source_updated_at
FROM source_postings p
JOIN source_boards b USING (board_id)
JOIN employers e USING (employer_id)
JOIN posting_screenings ps USING (posting_id)
JOIN posting_versions v ON v.version_id = p.current_version_id
JOIN posting_observations co ON co.observation_id = p.current_observation_id;

COMMENT ON VIEW listings IS
    'Every listing with stored content. Postings first seen before content was kept for all '
    'listings appear once they are observed again.';
