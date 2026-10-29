-- One row per request sent to the model, with its outcome and recorded token usage.
select
    attempt_id, job_id, run_id as extraction_run_id, model, started_at, finished_at,
    outcome as attempt_outcome, http_status, prompt_tokens, output_tokens, thought_tokens,
    total_tokens
from {{ source('ingestion', 'extraction_attempts') }}
