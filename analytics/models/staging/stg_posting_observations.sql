-- One row per sighting of a posting in a board scan. Repeated sightings are not new jobs.
select
    observation_id,
    board_scan_id,
    posting_id,
    version_id,
    observed_at,
    source_published_at,
    source_created_at,
    recorded_by
from {{ source('ingestion', 'posting_observations') }}
