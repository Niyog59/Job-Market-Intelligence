-- One row per board: how fresh its listings are, for the website.
--   last_complete_collected_at: the newest complete scan (every page read), i.e. the last
--     time the whole board was checked;
--   last_trusted_check_at: the newest scan the availability rules counted (Module 6);
--   on_hold: a mass-disappearance hold is active, so absences are not being counted.
with complete as (
    select board_id, max(collected_at) as last_complete_collected_at
    from {{ ref('stg_board_scans') }}
    where scan_status = 'complete'
    group by board_id
),

latest as (
    select distinct on (board_id)
        board_id, scan_status as latest_scan_status, started_at as latest_scan_started_at
    from {{ ref('stg_board_scans') }}
    order by board_id, started_at desc, board_scan_id desc
),

trusted as (
    select board_id, max(collected_at) as last_trusted_check_at
    from {{ ref('stg_absence_checks') }}
    where absence_outcome in ('qualifying', 'baseline', 'scope_changed')
    group by board_id
),

holds as (
    select distinct on (board_id) board_id, hold_id, opened_at as hold_opened_at
    from {{ ref('stg_board_holds') }}
    where hold_status = 'active'
    order by board_id, opened_at desc
)

select
    b.board_id,
    b.employer_id,
    e.employer_name,
    b.source_system,
    b.board_token,
    b.is_enabled,
    c.last_complete_collected_at,
    l.latest_scan_status,
    l.latest_scan_started_at,
    t.last_trusted_check_at,
    h.hold_id is not null as on_hold,
    h.hold_opened_at
from {{ ref('stg_boards') }} b
join {{ ref('stg_employers') }} e on e.employer_id = b.employer_id
left join complete c on c.board_id = b.board_id
left join latest l on l.board_id = b.board_id
left join trusted t on t.board_id = b.board_id
left join holds h on h.board_id = b.board_id
