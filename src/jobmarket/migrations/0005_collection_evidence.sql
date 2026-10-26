-- Evidence kept apart from the collected listings: official employer-to-board mapping
-- checks, and checks of individual job and application pages. Neither changes a board's
-- verification status or a posting's history; they record what was checked, when, and
-- what came back.

-- Was the board token found on the employer's official careers page?
CREATE TABLE board_mapping_checks (
    mapping_check_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    board_id          bigint NOT NULL REFERENCES source_boards (board_id),
    checked_at        timestamptz NOT NULL,
    url               text NOT NULL,
    http_status       integer CHECK (http_status BETWEEN 100 AND 599),
    final_url         text,
    outcome           text NOT NULL CHECK (outcome IN (
                          'found', 'not_found', 'refused', 'fetch_failed')),
    matched           text,
    detail            text
);

CREATE INDEX board_mapping_checks_board_idx ON board_mapping_checks (board_id, checked_at DESC);

COMMENT ON TABLE board_mapping_checks IS
    'found: the page references this exact board. not_found: the page loaded without such a '
    'reference (it may load jobs with scripts). refused: the site declined automated access '
    '(for example HTTP 403); this is not worked around. fetch_failed: no usable page.';

-- Was a posting's own page, or its application page, reachable when it was checked?
CREATE TABLE posting_page_checks (
    page_check_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    posting_id     bigint NOT NULL REFERENCES source_postings (posting_id),
    version_id     bigint NOT NULL REFERENCES posting_versions (version_id),
    url_kind       text NOT NULL CHECK (url_kind IN ('posting', 'application')),
    url            text NOT NULL,
    checked_at     timestamptz NOT NULL,
    http_status    integer CHECK (http_status BETWEEN 100 AND 599),
    final_url      text,
    outcome        text NOT NULL CHECK (outcome IN (
                       'available', 'not_found', 'redirected', 'refused', 'rate_limited',
                       'server_error', 'unreachable')),
    detail         text
);

CREATE INDEX posting_page_checks_posting_idx ON posting_page_checks (posting_id, checked_at DESC);

COMMENT ON TABLE posting_page_checks IS
    'A sampled check of one page with a plain GET; applications are never submitted. '
    'available means the page loaded at check time, not that the role is funded or '
    'interviewing. refused, rate_limited, server_error, and unreachable are access or '
    'collection problems and never mean a job closed.';
