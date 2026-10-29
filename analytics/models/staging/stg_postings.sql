-- One row per source posting (one job on one board). Current state only; history is in
-- versions, observations, and availability events.
select
    posting_id,
    board_id,
    source_posting_id,
    identity_basis,
    first_discovered_at,
    last_observed_at,
    current_version_id,
    availability_state as current_availability_state,
    qualifying_absences as current_qualifying_absences
from {{ source('ingestion', 'source_postings') }}
