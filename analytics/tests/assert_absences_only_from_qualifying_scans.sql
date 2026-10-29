-- A posting can only be counted missing (and later assumed inactive) by a qualifying scan:
-- failed, partial, held, or baseline scans never produce absence events.
select e.event_id, e.event_kind, a.absence_outcome
from {{ ref('stg_availability_events') }} e
left join {{ ref('stg_absence_checks') }} a on a.board_scan_id = e.board_scan_id
where e.event_kind in ('possibly_unavailable', 'absence_counted', 'assumed_inactive')
  and coalesce(a.absence_outcome, 'none') <> 'qualifying'
