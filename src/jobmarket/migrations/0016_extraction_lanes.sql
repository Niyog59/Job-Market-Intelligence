-- Extraction by more than one process at a time (a long-running worker, the hourly batch, a
-- manual run, an evaluation), with two lanes sharing one daily allowance.
--
-- Jobs. next_attempt_at lets a job wait for a retry without holding up other jobs;
-- round_attempts counts the requests of the current round (a round fails after four);
-- claimed_by and claimed_at record which process last took the job. The claim itself is a
-- session-level advisory lock on the job, which PostgreSQL releases when that process's
-- connection ends, so a crashed process never leaves a job stuck as running.
ALTER TABLE extraction_jobs
    ADD COLUMN next_attempt_at timestamptz,
    ADD COLUMN round_attempts smallint NOT NULL DEFAULT 0 CHECK (round_attempts >= 0),
    ADD COLUMN claimed_by text,
    ADD COLUMN claimed_at timestamptz;

-- Attempts are recorded when the request starts, so every process that paces the
-- per-minute limits or counts today's requests also sees requests still waiting for a
-- reply. finished_at and outcome stay empty until the reply or the failure arrives;
-- 'interrupted' marks a request whose process ended before recording it.
-- estimated_tokens is the pre-request estimate, used for requests still in flight.
-- lane is the lane that spent the request; empty for evaluation runs and for requests sent
-- before lanes existed.
ALTER TABLE extraction_attempts
    ALTER COLUMN finished_at DROP NOT NULL,
    ALTER COLUMN outcome DROP NOT NULL,
    ADD COLUMN estimated_tokens integer CHECK (estimated_tokens >= 0),
    ADD COLUMN lane text CHECK (lane IN ('new', 'backlog'));
ALTER TABLE extraction_attempts DROP CONSTRAINT extraction_attempts_outcome_check;
ALTER TABLE extraction_attempts ADD CONSTRAINT extraction_attempts_outcome_check CHECK (outcome IN (
    'success', 'invalid_output', 'rate_limited', 'quota_exhausted', 'server_error',
    'network_error', 'client_error', 'blocked', 'interrupted'));
ALTER TABLE extraction_attempts ADD CONSTRAINT extraction_attempts_finished_with_outcome
    CHECK ((finished_at IS NULL) = (outcome IS NULL));

COMMENT ON COLUMN extraction_jobs.next_attempt_at IS
    'Earliest time the job may be tried again (a scheduled retry); empty means now.';
COMMENT ON COLUMN extraction_attempts.lane IS
    'new (new or edited content) or backlog; empty for evaluations and older requests.';
