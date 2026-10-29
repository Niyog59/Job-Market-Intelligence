-- One row per judged board scan: how it counted (or did not count) toward absences.
select board_scan_id, board_id, collected_at, outcome as absence_outcome, hold_id
from {{ source('ingestion', 'absence_checks') }}
