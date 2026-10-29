-- Grain: one row per board. Its latest scan, its latest complete (trusted) collection and
-- how long ago that was, its initial-import baseline, and any active hold. A board whose
-- last complete collection is old is reported as such; its postings are carried forward,
-- not freshly verified.
with latest as (
    select distinct on (board_id) board_id, scan_status, started_at, collected_at
    from {{ ref('stg_board_scans') }}
    order by board_id, started_at desc, board_scan_id desc
),

complete as (
    select board_id, max(collected_at) as last_complete_collected_at
    from {{ ref('stg_board_scans') }}
    where scan_status = 'complete'
    group by 1
),

holds as (
    select board_id, count(*) as active_holds
    from {{ ref('stg_board_holds') }}
    where hold_status = 'active'
    group by 1
)

select
    bb.board_id,
    e.employer_name,
    bb.source_system,
    bb.board_token,
    bb.is_enabled,
    bb.baseline_collected_at,
    l.scan_status as latest_scan_status,
    l.started_at as latest_scan_started_at,
    c.last_complete_collected_at,
    round(extract(epoch from now() - c.last_complete_collected_at) / 3600.0, 1)
        as hours_since_complete_collection,
    coalesce(h.active_holds, 0) > 0 as on_hold
from {{ ref('int_board_baselines') }} bb
join {{ ref('stg_employers') }} e on e.employer_id = bb.employer_id
left join latest l on l.board_id = bb.board_id
left join complete c on c.board_id = bb.board_id
left join holds h on h.board_id = bb.board_id
