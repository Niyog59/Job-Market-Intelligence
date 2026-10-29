-- One row: when each kind of history starts. Before these instants the evidence does not
-- exist, so reports show those days as not covered instead of guessing. Availability is
-- tracked from the earliest availability event: on this installation the start-fresh
-- backfill of 2026-10-27, on a new installation the first collection.
select
    (select min(occurred_at) from {{ ref('stg_availability_events') }})
        as availability_tracking_started_at,
    (select min(created_at) from {{ ref('stg_extraction_results') }}) as first_extraction_at,
    (select min(collected_at) from {{ ref('stg_board_scans') }}) as first_collection_at
