-- One row per availability state change, timed by the collection time of the scan that
-- caused it (occurred_at). event_kind separates the tracking start (a backfill, not news)
-- from first sightings, absences, reappearances, and closures.
select
    event_id,
    posting_id,
    from_state,
    to_state,
    qualifying_absences,
    reason,
    board_scan_id,
    occurred_at,
    recorded_at,
    case
        when reason like 'availability tracking started%' then 'tracking_started'
        when from_state is null then 'first_observed'
        when to_state = 'closed' then 'closed'
        when to_state = 'observed' then 'reappeared'
        when to_state = 'assumed_inactive' then 'assumed_inactive'
        when to_state = 'possibly_unavailable' and from_state = 'observed'
            then 'possibly_unavailable'
        when to_state = 'possibly_unavailable' then 'absence_counted'
        else 'other'
    end as event_kind
from {{ source('ingestion', 'posting_availability_events') }}
