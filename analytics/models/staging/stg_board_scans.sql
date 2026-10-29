-- One row per board scan. collected_at is when the scan's first page was fetched: the time
-- every observation from the scan is recorded under (null when no response arrived).
with first_pages as (
    select board_scan_id, min(fetched_at) as collected_at
    from {{ source('ingestion', 'raw_responses') }}
    group by board_scan_id
)

select
    s.board_scan_id,
    s.run_id,
    s.board_id,
    s.status as scan_status,
    s.attempt_kind,
    s.started_at,
    s.finished_at,
    f.collected_at,
    s.pages_fetched,
    s.jobs_received,
    s.jobs_invalid,
    s.error_message
from {{ source('ingestion', 'board_scans') }} s
left join first_pages f using (board_scan_id)
