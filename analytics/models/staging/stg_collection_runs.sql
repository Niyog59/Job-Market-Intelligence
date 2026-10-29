-- One row per collection run (collect or retry-due).
select run_id, trigger as run_trigger, started_at, finished_at, status as run_status
from {{ source('ingestion', 'collection_runs') }}
