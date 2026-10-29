-- Reporting days have no gaps, and only the last day (today) is provisional.
with d as (
    select report_day, is_complete,
           lag(report_day) over (order by report_day) as previous_day,
           report_day = max(report_day) over () as is_last
    from {{ ref('int_reporting_days') }}
)
select * from d
where (previous_day is not null and report_day <> previous_day + 1)
   or (is_complete = is_last)
