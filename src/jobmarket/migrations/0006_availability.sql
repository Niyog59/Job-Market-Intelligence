-- Availability verification: whether each source posting is still listed, judged only from
-- trustworthy evidence, with board-level holds against mass disappearances.
--
-- States: observed (in a valid source response), possibly_unavailable (missing from one or
-- two qualifying scans; still shown, with a warning), assumed_inactive (missing from a third
-- qualifying scan; hidden from the default active feed), closed (official evidence that
-- applications are closed; hidden at once). Nothing is ever deleted; hiding is a state.

-- How a board was read. A scan under a different scope than the board's comparison
-- reference never counts absences; it becomes the new reference instead.
ALTER TABLE board_scans ADD COLUMN scope_key text;

COMMENT ON COLUMN board_scans.scope_key IS
    'Source system, hosting instance, and request settings used for this scan. Two scans '
    'are comparable for absence counting only if their scope keys are equal.';

ALTER TABLE source_postings
    ADD COLUMN availability_state text NOT NULL DEFAULT 'observed'
        CHECK (availability_state IN
            ('observed', 'possibly_unavailable', 'assumed_inactive', 'closed')),
    ADD COLUMN qualifying_absences integer NOT NULL DEFAULT 0
        CHECK (qualifying_absences >= 0),
    ADD COLUMN availability_changed_at timestamptz;

COMMENT ON COLUMN source_postings.qualifying_absences IS
    'Qualifying scans (complete, same scope, no board hold, no mass disappearance) of this '
    'board in a row in which the posting was missing. Reset when it reappears.';
COMMENT ON COLUMN source_postings.complete_scan_absences IS
    'Legacy Module 3 count of complete scans without this posting. Kept as history; '
    'availability uses qualifying_absences.';

-- Every state change, with its reason and evidence.
CREATE TABLE posting_availability_events (
    event_id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    posting_id           bigint NOT NULL REFERENCES source_postings (posting_id),
    from_state           text,
    to_state             text NOT NULL CHECK (to_state IN
                             ('observed', 'possibly_unavailable', 'assumed_inactive', 'closed')),
    qualifying_absences  integer NOT NULL CHECK (qualifying_absences >= 0),
    reason               text NOT NULL,
    board_scan_id        bigint REFERENCES board_scans (board_scan_id),
    evidence             jsonb,
    occurred_at          timestamptz NOT NULL,
    recorded_at          timestamptz NOT NULL
);

CREATE INDEX posting_availability_events_posting_idx
    ON posting_availability_events (posting_id, event_id);

COMMENT ON COLUMN posting_availability_events.occurred_at IS
    'Collection time of the scan that caused the change, or the time of the manual review.';

-- Which postings a complete scan listed: the comparison reference for later scans.
CREATE TABLE board_scan_listings (
    board_scan_id  bigint NOT NULL REFERENCES board_scans (board_scan_id),
    posting_id     bigint NOT NULL REFERENCES source_postings (posting_id),
    PRIMARY KEY (board_scan_id, posting_id)
);

-- A board-level hold blocks absence counting while a scan looks like a mass disappearance.
CREATE TABLE board_holds (
    hold_id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    board_id               bigint NOT NULL REFERENCES source_boards (board_id),
    opened_at              timestamptz NOT NULL,
    opened_by_scan_id      bigint NOT NULL REFERENCES board_scans (board_scan_id),
    reference_scan_id      bigint NOT NULL REFERENCES board_scans (board_scan_id),
    trigger                text NOT NULL CHECK (trigger IN ('all_missing', 'share_missing')),
    reference_count        integer NOT NULL CHECK (reference_count > 0),
    missing_count          integer NOT NULL CHECK (missing_count > 0),
    thresholds             jsonb NOT NULL,
    status                 text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'released')),
    released_at            timestamptz,
    release_reason         text CHECK (release_reason IN
                               ('collection_issue_resolved', 'source_removals_verified')),
    release_evidence       text,
    new_reference_scan_id  bigint REFERENCES board_scans (board_scan_id),
    CONSTRAINT board_holds_release_complete CHECK (
        (status = 'released') = (released_at IS NOT NULL AND release_reason IS NOT NULL
                                 AND release_evidence IS NOT NULL))
);

CREATE UNIQUE INDEX board_holds_one_active_per_board ON board_holds (board_id)
    WHERE status = 'active';

-- How each scan was judged for absence counting. One row per scan, so a replay can never
-- count the same scan twice.
CREATE TABLE absence_checks (
    board_scan_id      bigint PRIMARY KEY REFERENCES board_scans (board_scan_id),
    board_id           bigint NOT NULL REFERENCES source_boards (board_id),
    collected_at       timestamptz,
    evaluated_at       timestamptz NOT NULL,
    scope_key          text,
    outcome            text NOT NULL CHECK (outcome IN (
                           'qualifying', 'baseline', 'scope_changed', 'not_complete',
                           'held', 'mass_disappearance')),
    reference_scan_id  bigint REFERENCES board_scans (board_scan_id),
    reference_count    integer,
    present_count      integer,
    missing_count      integer,
    hold_id            bigint REFERENCES board_holds (hold_id),
    detail             text
);

CREATE INDEX absence_checks_board_idx ON absence_checks (board_id, collected_at DESC);

COMMENT ON COLUMN absence_checks.outcome IS
    'qualifying: absences counted. baseline: first comparable scan, becomes the reference. '
    'scope_changed: read differently than the reference; becomes the new reference. '
    'not_complete: failed or partial; nothing counted. held: a board hold is active. '
    'mass_disappearance: this scan opened a hold.';

-- Each board's current comparison reference.
ALTER TABLE source_boards
    ADD COLUMN absence_reference_scan_id bigint REFERENCES board_scans (board_scan_id),
    ADD COLUMN absence_scope_key text;

-- Start fresh (decision 2026-10-27): every existing posting is observed with no qualifying
-- absences. Earlier complete-scan counts stay in complete_scan_absences as history.
UPDATE source_postings SET availability_changed_at = last_observed_at;

INSERT INTO posting_availability_events (posting_id, from_state, to_state,
    qualifying_absences, reason, occurred_at, recorded_at)
SELECT posting_id, NULL, 'observed', 0,
       'availability tracking started; earlier absences are not counted', now(), now()
FROM source_postings;

-- Page checks can now report an explicit closure notice on the employer's job page.
ALTER TABLE posting_page_checks DROP CONSTRAINT posting_page_checks_outcome_check;
ALTER TABLE posting_page_checks ADD CONSTRAINT posting_page_checks_outcome_check
    CHECK (outcome IN ('available', 'closed_notice', 'not_found', 'redirected', 'refused',
                       'rate_limited', 'server_error', 'unreachable'));

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
    p.availability_changed_at
FROM source_postings p
JOIN posting_screenings ps USING (posting_id)
JOIN source_boards b USING (board_id)
JOIN employers e USING (employer_id)
JOIN posting_versions v ON v.version_id = p.current_version_id
JOIN posting_observations co ON co.observation_id = p.current_observation_id
WHERE ps.retained;

-- The default feed: retained jobs that are observed or only possibly unavailable.
CREATE VIEW active_jobs AS
SELECT * FROM retained_jobs
WHERE availability_state IN ('observed', 'possibly_unavailable');

DROP VIEW board_status;
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
      WHERE p.board_id = b.board_id AND ps.retained) AS retained_postings,
    (SELECT max(c.collected_at) FROM absence_checks c
      WHERE c.board_id = b.board_id
        AND c.outcome IN ('qualifying', 'baseline', 'scope_changed')) AS last_trusted_check_at,
    h.hold_id,
    h.opened_at AS hold_opened_at,
    h.trigger AS hold_trigger,
    h.missing_count AS hold_missing_count,
    h.reference_count AS hold_reference_count
FROM source_boards b
JOIN employers e USING (employer_id)
LEFT JOIN latest_board_scans ls ON ls.board_id = b.board_id
LEFT JOIN collection_incidents i ON i.board_id = b.board_id AND i.status <> 'resolved'
LEFT JOIN board_holds h ON h.board_id = b.board_id AND h.status = 'active';
