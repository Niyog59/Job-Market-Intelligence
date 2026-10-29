-- Grain: one row per reporting day per population ('all' listings, or 'retained' by the
-- screening rules as of that day).
--   initial_imports: postings first seen as part of their board's initial import that day
--     (they count toward inventory, never as hiring);
--   discoveries: postings first observed by the pipeline that day after their board's
--     baseline (not necessarily newly published by the employer);
--   updates: meaningful content changes (a new version of an existing posting), including
--     employer-wide template edits that touch many postings at once;
--   became_possibly_unavailable, assumed_inactive, explicit_closures, reactivations:
--     availability transitions timed by the scan that caused them.
-- Repeated observations of the same posting are never counted. boards_baselined shows
-- coverage growing, so new boards are not mistaken for market growth.
with days as (select * from {{ ref('int_reporting_days') }}),

populations as (select unnest(array['all', 'retained']) as population),

posting_days as (select * from {{ ref('int_posting_days') }}),

firsts as (
    select pd.report_day, pd.cohort, pd.retained
    from posting_days pd
    where pd.report_day = pd.discovery_day
),

updates as (
    select {{ reporting_day('v.first_seen_at') }} as report_day, pd.retained
    from {{ ref('stg_posting_versions') }} v
    join posting_days pd
        on pd.posting_id = v.posting_id
       and pd.report_day = {{ reporting_day('v.first_seen_at') }}
    where v.version_no > 1
),

transitions as (
    select {{ reporting_day('e.occurred_at') }} as report_day, e.event_kind, pd.retained
    from {{ ref('stg_availability_events') }} e
    join posting_days pd
        on pd.posting_id = e.posting_id
       and pd.report_day = {{ reporting_day('e.occurred_at') }}
    where e.event_kind in ('possibly_unavailable', 'assumed_inactive', 'closed', 'reappeared')
),

baselines as (
    select baseline_day as report_day, count(*) as boards_baselined
    from {{ ref('int_board_baselines') }}
    where baseline_day is not null
    group by 1
)

select
    d.report_day,
    p.population,
    d.is_complete,
    (select count(*) from firsts f where f.report_day = d.report_day
        and f.cohort = 'initial_import' and (p.population = 'all' or f.retained))
        as initial_imports,
    (select count(*) from firsts f where f.report_day = d.report_day
        and f.cohort = 'discovered' and (p.population = 'all' or f.retained))
        as discoveries,
    (select count(*) from updates u where u.report_day = d.report_day
        and (p.population = 'all' or u.retained)) as updates,
    (select count(*) from transitions t where t.report_day = d.report_day
        and t.event_kind = 'possibly_unavailable' and (p.population = 'all' or t.retained))
        as became_possibly_unavailable,
    (select count(*) from transitions t where t.report_day = d.report_day
        and t.event_kind = 'assumed_inactive' and (p.population = 'all' or t.retained))
        as assumed_inactive,
    (select count(*) from transitions t where t.report_day = d.report_day
        and t.event_kind = 'closed' and (p.population = 'all' or t.retained))
        as explicit_closures,
    (select count(*) from transitions t where t.report_day = d.report_day
        and t.event_kind = 'reappeared' and (p.population = 'all' or t.retained))
        as reactivations,
    coalesce(b.boards_baselined, 0) as boards_baselined,
    (select coalesce(sum(boards_baselined), 0)::int from baselines b2
        where b2.report_day <= d.report_day) as boards_with_baseline
from days d
cross join populations p
left join baselines b on b.report_day = d.report_day
