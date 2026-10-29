-- Requirement extraction: a persistent queue of posting versions to send to the language
-- model, every request with its usage, and every validated result with its versions and
-- evidence. Collection never waits for, or depends on, any of it.

-- The exact text sent for a posting version. Evidence quotes are located in this text, so
-- results keep pointing at what the model actually read.
CREATE TABLE extraction_inputs (
    input_hash       text PRIMARY KEY CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    prompt_version   text NOT NULL,
    document         text NOT NULL,
    char_count       integer NOT NULL CHECK (char_count >= 0),
    created_at       timestamptz NOT NULL
);

-- One unit of work: extract one posting version under one configuration (model, thinking
-- level, prompt version, schema version).
CREATE TABLE extraction_jobs (
    job_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version_id       bigint NOT NULL REFERENCES posting_versions (version_id),
    posting_id       bigint NOT NULL REFERENCES source_postings (posting_id),
    config_key       text NOT NULL,
    input_hash       text REFERENCES extraction_inputs (input_hash),
    priority         smallint NOT NULL CHECK (priority BETWEEN 1 AND 4),
    reason           text NOT NULL CHECK (reason IN ('new_content', 'backlog', 'reprocess')),
    status           text NOT NULL DEFAULT 'pending' CHECK (status IN (
                         'pending', 'running', 'succeeded', 'failed', 'gave_up', 'skipped',
                         'superseded')),
    skip_reason      text CHECK (skip_reason IN ('too_long', 'no_description')),
    runs_failed      integer NOT NULL DEFAULT 0 CHECK (runs_failed >= 0),
    last_error       text,
    note             text,
    result_id        bigint,
    created_at       timestamptz NOT NULL,
    updated_at       timestamptz NOT NULL,
    CONSTRAINT extraction_jobs_version_config_key UNIQUE (version_id, config_key),
    CONSTRAINT extraction_jobs_skip_reason CHECK ((status = 'skipped') = (skip_reason IS NOT NULL))
);

CREATE INDEX extraction_jobs_queue_idx ON extraction_jobs (priority, created_at, job_id)
    WHERE status IN ('pending', 'failed');

COMMENT ON COLUMN extraction_jobs.priority IS
    '1 new or edited retained listing, 2 retained backlog, 3 new or edited other listing, '
    '4 other backlog (decision 2026-10-27). Lower runs first.';
COMMENT ON COLUMN extraction_jobs.note IS
    'Why the job was created by jobmarket extract reprocess (the reason given).';
COMMENT ON COLUMN extraction_jobs.runs_failed IS
    'Extraction runs in which every request for this job failed. At 5 the job gives up and '
    'waits for jobmarket extract reprocess.';

-- One invocation of jobmarket extract run.
CREATE TABLE extraction_runs (
    run_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at       timestamptz NOT NULL,
    finished_at      timestamptz,
    config_key       text NOT NULL,
    requests         integer NOT NULL DEFAULT 0,
    succeeded        integer NOT NULL DEFAULT 0,
    reused           integer NOT NULL DEFAULT 0,
    failed           integer NOT NULL DEFAULT 0,
    stop_reason      text
);

-- Every request sent to the model, successful or not, with its token usage. Daily usage
-- and the safety stop are computed from these rows.
CREATE TABLE extraction_attempts (
    attempt_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id           bigint NOT NULL REFERENCES extraction_jobs (job_id),
    run_id           bigint NOT NULL REFERENCES extraction_runs (run_id),
    attempt_no       integer NOT NULL CHECK (attempt_no >= 1),
    model            text NOT NULL,
    started_at       timestamptz NOT NULL,
    finished_at      timestamptz NOT NULL,
    outcome          text NOT NULL CHECK (outcome IN (
                         'success', 'invalid_output', 'rate_limited', 'quota_exhausted',
                         'server_error', 'network_error', 'client_error', 'blocked')),
    http_status      integer,
    quota_id         text,
    retry_after_seconds numeric,
    prompt_tokens    integer,
    output_tokens    integer,
    thought_tokens   integer,
    total_tokens     integer,
    traffic_type     text,
    model_version    text,
    raw_reply        text,
    error_detail     text
);

CREATE INDEX extraction_attempts_started_idx ON extraction_attempts (started_at);

-- A validated extraction of one posting version. Results are never updated in place except
-- by re-normalization (normalized, derived, normalization_version), which needs no model
-- call; the model's own answer (raw_output) never changes.
CREATE TABLE extraction_results (
    result_id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id                 bigint NOT NULL REFERENCES extraction_jobs (job_id),
    version_id             bigint NOT NULL REFERENCES posting_versions (version_id),
    posting_id             bigint NOT NULL REFERENCES source_postings (posting_id),
    input_hash             text NOT NULL REFERENCES extraction_inputs (input_hash),
    config_key             text NOT NULL,
    model                  text NOT NULL,
    model_version          text,
    thinking_level         text NOT NULL,
    prompt_version         text NOT NULL,
    schema_version         text NOT NULL,
    normalization_version  text NOT NULL,
    raw_output             jsonb NOT NULL,
    normalized             jsonb NOT NULL,
    derived                jsonb NOT NULL,
    validation_status      text NOT NULL CHECK (validation_status IN ('valid', 'valid_with_warnings')),
    validation_issues      jsonb NOT NULL,
    evidence               jsonb NOT NULL,
    reused_from_result_id  bigint REFERENCES extraction_results (result_id),
    attempt_id             bigint REFERENCES extraction_attempts (attempt_id),
    created_at             timestamptz NOT NULL,
    renormalized_at        timestamptz,
    CONSTRAINT extraction_results_version_config_key UNIQUE (version_id, config_key),
    CONSTRAINT extraction_results_source CHECK (
        (reused_from_result_id IS NULL) <> (attempt_id IS NULL))
);

CREATE INDEX extraction_results_input_idx ON extraction_results (input_hash, config_key);

ALTER TABLE extraction_jobs ADD CONSTRAINT extraction_jobs_result_fkey
    FOREIGN KEY (result_id) REFERENCES extraction_results (result_id);

COMMENT ON COLUMN extraction_results.evidence IS
    'Every quote the model gave, with where it was found in the input document (start and '
    'end character offsets) or found = false.';
COMMENT ON COLUMN extraction_results.reused_from_result_id IS
    'Set when an identical input under the same configuration was already extracted for '
    'another version: the answer is copied, no request is sent.';

-- A pause set when the model's daily quota is exhausted. Extraction resumes after it;
-- collection is never paused.
CREATE TABLE extraction_pauses (
    model            text PRIMARY KEY,
    paused_until     timestamptz NOT NULL,
    reason           text NOT NULL,
    set_at           timestamptz NOT NULL
);

-- When extraction tracking started: versions recorded before this are backlog.
CREATE TABLE extraction_settings (
    singleton        boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    tracking_started_at timestamptz NOT NULL
);
INSERT INTO extraction_settings (tracking_started_at) VALUES (now());

-- The extraction of each listing's current content, if there is one. A result for an
-- older version is never shown as current: it joins on the current version only.
CREATE VIEW current_extractions AS
SELECT DISTINCT ON (p.posting_id)
    p.posting_id,
    r.result_id,
    r.version_id,
    r.config_key,
    r.model,
    r.validation_status,
    r.normalized,
    r.derived,
    r.created_at
FROM source_postings p
JOIN extraction_results r ON r.version_id = p.current_version_id
ORDER BY p.posting_id, r.created_at DESC, r.result_id DESC;

-- Requests per Pacific day (the day the provider's daily quota uses).
CREATE VIEW extraction_usage_daily AS
SELECT (started_at AT TIME ZONE 'America/Los_Angeles')::date AS pacific_day,
       model,
       count(*) AS requests,
       count(*) FILTER (WHERE outcome = 'success') AS successful,
       coalesce(sum(prompt_tokens), 0) AS prompt_tokens,
       coalesce(sum(output_tokens), 0) AS output_tokens,
       coalesce(sum(thought_tokens), 0) AS thought_tokens,
       coalesce(sum(total_tokens), 0) AS total_tokens
FROM extraction_attempts
GROUP BY 1, 2;

-- Listings where the AI extraction and the rule-based screening disagree about whether
-- the role and place match. Rules keep deciding retention; this is for review.
CREATE VIEW screening_disagreements AS
SELECT l.posting_id, l.employer, l.title, l.location_text, l.retained AS rules_retained,
       (c.derived ->> 'ai_would_retain')::boolean AS ai_would_retain,
       l.rule_families, c.derived -> 'role_categories' AS ai_categories,
       l.rule_texas_eligibility, c.derived ->> 'place' AS ai_place,
       c.result_id
FROM listings l
JOIN current_extractions c USING (posting_id)
WHERE l.retained IS DISTINCT FROM (c.derived ->> 'ai_would_retain')::boolean;
