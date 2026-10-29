-- One row per extraction job (a posting version queued under one configuration).
select
    job_id, version_id, posting_id, config_key, priority, reason as queue_reason,
    status as job_status, skip_reason, runs_failed, result_id, created_at, updated_at
from {{ source('ingestion', 'extraction_jobs') }}
