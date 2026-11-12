-- A job gets three tries instead of rounds (decision 2026-11-11). failure_kinds lists the
-- reason of each failed try that counted against the job (invalid_output, blocked,
-- client_error); provider outages never count. When two tries fail for the same reason the
-- job records it in repeated_error and gets one fourth try.
ALTER TABLE extraction_jobs
    ADD COLUMN failure_kinds text[] NOT NULL DEFAULT '{}',
    ADD COLUMN repeated_error text,
    ADD COLUMN repeated_error_at timestamptz;

COMMENT ON COLUMN extraction_jobs.failure_kinds IS
    'Reason of each failed try that counted (oldest first). Three tries, four after a repeat.';
COMMENT ON COLUMN extraction_jobs.repeated_error IS
    'The error of the first try that failed for the same reason as an earlier one.';
COMMENT ON COLUMN extraction_jobs.runs_failed IS
    'Failed rounds under the rules before migration 0018; no longer updated.';
COMMENT ON COLUMN extraction_jobs.round_attempts IS
    'Requests of the current round under the rules before migration 0018; no longer updated.';

-- Jobs waiting for their next round start over with three tries under the new rule.
UPDATE extraction_jobs SET status = 'pending', next_attempt_at = NULL
WHERE status = 'failed';
