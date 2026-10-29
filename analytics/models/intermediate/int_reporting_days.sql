-- One row per reporting day (calendar day in the reporting time zone, America/Chicago),
-- from the first collection to today. day_end is the instant the day ends; as_of is the
-- moment its numbers describe (day_end for completed days, now for today). Today is
-- provisional: its numbers can still change as the day goes on.
with bounds as (
    select
        {{ reporting_day('min(started_at)') }} as first_day,
        {{ reporting_day('now()') }} as today
    from {{ ref('stg_board_scans') }}
)

select
    d::date as report_day,
    {{ day_end('d::date') }} as day_end,
    least({{ day_end('d::date') }}, now()) as as_of,
    d::date < bounds.today as is_complete
from bounds
cross join lateral generate_series(bounds.first_day, bounds.today, interval '1 day') as d
