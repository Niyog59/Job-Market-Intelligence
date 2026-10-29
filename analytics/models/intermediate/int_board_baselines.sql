-- One row per board: its first collection and its first COMPLETE collection (the baseline).
-- Everything a board already listed by its baseline is an initial import, not new hiring;
-- a partial first scan does not establish the baseline.
select
    b.board_id,
    b.employer_id,
    b.source_system,
    b.board_token,
    b.is_enabled,
    min(s.collected_at) as first_collected_at,
    min(s.collected_at) filter (where s.scan_status = 'complete') as baseline_collected_at,
    {{ reporting_day("min(s.collected_at) filter (where s.scan_status = 'complete')") }}
        as baseline_day
from {{ ref('stg_boards') }} b
left join {{ ref('stg_board_scans') }} s on s.board_id = b.board_id
group by b.board_id, b.employer_id, b.source_system, b.board_token, b.is_enabled
