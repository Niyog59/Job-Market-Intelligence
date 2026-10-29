-- Grain: one row per reporting day per population ('all' or 'retained') per availability
-- state, as known at the end of that day (today: provisional, as of now).
--   source_records: postings (one job on one board);
--   opportunities: duplicate groups counted once plus ungrouped postings;
--   seen_that_day / carried_forward: postings observed in a scan that day, versus carried
--     from an earlier sighting (still listed as far as we know, but not re-verified that
--     day);
--   coverage: days before availability tracking started have state not_tracked and are not
--     reconstructed.
-- A later closure never changes an earlier day's row.
with populations as (select unnest(array['all', 'retained']) as population),

postings as (
    select
        pd.report_day,
        p.population,
        coalesce(pd.availability_state, 'not_tracked') as availability_state,
        count(*) as source_records,
        count(*) filter (where pd.observed_this_day) as seen_that_day,
        count(*) filter (where not coalesce(pd.observed_this_day, false)) as carried_forward,
        count(*) filter (where pd.cohort = 'initial_import') as from_initial_imports
    from {{ ref('int_posting_days') }} pd
    cross join populations p
    where p.population = 'all' or pd.retained
    group by 1, 2, 3
),

opportunities as (
    select o.report_day, p.population, o.availability_state, count(*) as opportunities
    from {{ ref('int_opportunity_days') }} o
    cross join populations p
    where p.population = 'all' or o.retained
    group by 1, 2, 3
)

select
    coalesce(ps.report_day, o.report_day) as report_day,
    coalesce(ps.population, o.population) as population,
    coalesce(ps.availability_state, o.availability_state) as availability_state,
    d.is_complete,
    case when d.as_of >= tb.availability_tracking_started_at then 'tracked'
         else 'before_availability_tracking' end as coverage,
    coalesce(ps.source_records, 0) as source_records,
    coalesce(o.opportunities, 0) as opportunities,
    coalesce(ps.seen_that_day, 0) as seen_that_day,
    coalesce(ps.carried_forward, 0) as carried_forward,
    coalesce(ps.from_initial_imports, 0) as from_initial_imports
from postings ps
full join opportunities o
    on o.report_day = ps.report_day and o.population = ps.population
   and o.availability_state = ps.availability_state
join {{ ref('int_reporting_days') }} d on d.report_day = coalesce(ps.report_day, o.report_day)
cross join {{ ref('int_tracking_boundaries') }} tb
