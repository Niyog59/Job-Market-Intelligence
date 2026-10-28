-- Duplicate handling, kept apart from repeated ingestion.
--
-- Repeated ingestion of the same source record is prevented by identity (board + source job
-- id, unique). Cross-record matching is separate: strong evidence (the same employer and an
-- employer-issued requisition ID, or an identical job-specific application URL) groups
-- source records automatically; weaker similarity only flags a possible duplicate for review.
-- Every source record keeps its own identity, history, and availability; groups are
-- reviewable and reversible, and never change availability.

CREATE TABLE duplicate_groups (
    group_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rule             text NOT NULL CHECK (rule IN
                         ('requisition_id', 'application_url', 'manual_review')),
    rule_version     text NOT NULL,
    match_key        text NOT NULL,
    confidence       text NOT NULL CHECK (confidence IN ('strong', 'reviewed')),
    evidence         jsonb NOT NULL,
    created_at       timestamptz NOT NULL,
    status           text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'dissolved')),
    dissolved_at     timestamptz,
    dissolved_reason text,
    -- A dissolved group is remembered, so the same rule never recreates it.
    CONSTRAINT duplicate_groups_rule_key UNIQUE (rule, match_key),
    CONSTRAINT duplicate_groups_dissolved_has_reason CHECK (
        (status = 'dissolved') = (dissolved_at IS NOT NULL AND dissolved_reason IS NOT NULL))
);

COMMENT ON COLUMN duplicate_groups.confidence IS
    'strong: grouped automatically on strong evidence. reviewed: grouped by a person after '
    'review. Categories only; no probability is implied.';

CREATE TABLE duplicate_group_members (
    group_id    bigint NOT NULL REFERENCES duplicate_groups (group_id),
    posting_id  bigint NOT NULL REFERENCES source_postings (posting_id),
    added_at    timestamptz NOT NULL,
    PRIMARY KEY (group_id, posting_id)
);

CREATE INDEX duplicate_group_members_posting_idx ON duplicate_group_members (posting_id);

-- Possible duplicates: flagged for review, never merged.
ALTER TABLE posting_duplicate_flags
    ADD COLUMN flag_id bigint GENERATED ALWAYS AS IDENTITY,
    ADD COLUMN rule text NOT NULL DEFAULT 'same_title_description',
    ADD COLUMN rule_version text NOT NULL DEFAULT 'module-3',
    ADD COLUMN confidence text NOT NULL DEFAULT 'possible' CHECK (confidence = 'possible'),
    ADD COLUMN evidence jsonb,
    ADD COLUMN status text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'confirmed', 'dismissed', 'superseded')),
    ADD COLUMN reviewed_at timestamptz,
    ADD COLUMN review_reason text;

ALTER TABLE posting_duplicate_flags ADD CONSTRAINT posting_duplicate_flags_flag_id_key
    UNIQUE (flag_id);

-- The Module 3 rule (same employer, title, and identical description, ignoring location)
-- is replaced by a rule that also requires a shared location. Its flags are kept, marked
-- superseded; the new rule reopens any pair it also matches.
UPDATE posting_duplicate_flags
SET status = 'superseded', reviewed_at = now(),
    review_reason = 'rule replaced by title, location, and description rule 2026-10-27.1';

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
    co.source_created_at,
    p.availability_state,
    p.qualifying_absences,
    p.availability_changed_at,
    p.identity_basis,
    (SELECT m.group_id FROM duplicate_group_members m
       JOIN duplicate_groups g USING (group_id)
      WHERE m.posting_id = p.posting_id AND g.status = 'active'
      ORDER BY m.group_id LIMIT 1) AS duplicate_group_id,
    (SELECT count(*) FROM posting_duplicate_flags f
      WHERE f.status = 'open'
        AND (f.posting_id = p.posting_id OR f.related_posting_id = p.posting_id))
        AS open_duplicate_flags
FROM source_postings p
JOIN posting_screenings ps USING (posting_id)
JOIN source_boards b USING (board_id)
JOIN employers e USING (employer_id)
JOIN posting_versions v ON v.version_id = p.current_version_id
JOIN posting_observations co ON co.observation_id = p.current_observation_id
WHERE ps.retained;

CREATE OR REPLACE VIEW active_jobs AS
SELECT * FROM retained_jobs
WHERE availability_state IN ('observed', 'possibly_unavailable');
