-- Grain: one row per week (Monday to Sunday, America/Chicago) per level rule ('ai' or
-- 'years') per level per role group per metro, for kept jobs (duplicate postings merged).
--   new_jobs: jobs first discovered that week, initial imports of newly added boards
--     excluded (they were already open, not new hiring); classified by what is known now.
--   open_jobs_end: jobs listed at the end of the week (or at the latest day for the current
--     week), classified by what was known then ("Not extracted" when the AI had not run yet).
--   genai_*: the same jobs whose title or description mentions generative AI.
-- Employer counts are within each row and do not add up across rows.
with jobs as (
    -- every kept job ever seen, by its earliest posting, with its latest known attributes
    select
        cp.opportunity_key,
        min(cp.first_discovered_at) as first_discovered_at,
        (array_agg(cp.cohort order by cp.first_discovered_at, cp.posting_id))[1] as cohort,
        (array_agg(cp.employer_id order by cp.last_observed_at desc, cp.posting_id desc))[1]
            as employer_id,
        (array_agg(cp.extraction_result_id order by cp.last_observed_at desc,
                   cp.posting_id desc))[1] as extraction_result_id,
        (array_agg(cp.version_id order by cp.last_observed_at desc, cp.posting_id desc))[1]
            as version_id
    from {{ ref('int_current_postings') }} cp
    where cp.retained
    group by cp.opportunity_key
),

new_jobs as (
    select
        date_trunc('week', {{ reporting_day('j.first_discovered_at') }})::date as week_start,
        j.opportunity_key,
        j.employer_id,
        coalesce(a.role_group, 'Not extracted') as role_group,
        coalesce(a.metro, 'Not extracted') as metro,
        coalesce(a.level_ai, 'Not extracted') as level_ai,
        coalesce(a.level_years, 'Not extracted') as level_years,
        coalesce(g.genai_mention, false) as genai_mention
    from jobs j
    left join {{ ref('int_job_attributes') }} a on a.result_id = j.extraction_result_id
    left join {{ ref('int_genai_mentions') }} g on g.version_id = j.version_id
    where j.cohort = 'discovered'
),

week_ends as (
    select date_trunc('week', report_day)::date as week_start, max(report_day) as report_day,
           bool_and(is_complete) as week_complete
    from {{ ref('int_reporting_days') }}
    where report_day <= (select max(report_day) from {{ ref('int_posting_days') }})
    group by 1
),

open_jobs as (
    select distinct on (w.week_start, coalesce('group:' || pd.duplicate_group_id,
                                               'posting:' || pd.posting_id))
        w.week_start,
        coalesce('group:' || pd.duplicate_group_id, 'posting:' || pd.posting_id)
            as opportunity_key,
        b.employer_id,
        coalesce(a.role_group, 'Not extracted') as role_group,
        coalesce(a.metro, 'Not extracted') as metro,
        coalesce(a.level_ai, 'Not extracted') as level_ai,
        coalesce(a.level_years, 'Not extracted') as level_years,
        coalesce(g.genai_mention, false) as genai_mention
    from week_ends w
    join {{ ref('int_posting_days') }} pd on pd.report_day = w.report_day
    join {{ ref('stg_boards') }} b on b.board_id = pd.board_id
    left join {{ ref('int_job_attributes') }} a on a.result_id = pd.extraction_result_id
    left join {{ ref('int_genai_mentions') }} g on g.version_id = pd.version_id
    where pd.retained and pd.availability_state in ('observed', 'possibly_unavailable')
    order by w.week_start, coalesce('group:' || pd.duplicate_group_id,
                                    'posting:' || pd.posting_id),
             pd.last_observed_at desc, pd.posting_id desc
),

by_rule as (
    select week_start, 'ai' as level_rule, level_ai as level, role_group, metro,
           opportunity_key, employer_id, genai_mention, 'new' as kind from new_jobs
    union all
    select week_start, 'years', level_years, role_group, metro,
           opportunity_key, employer_id, genai_mention, 'new' from new_jobs
    union all
    select week_start, 'ai', level_ai, role_group, metro,
           opportunity_key, employer_id, genai_mention, 'open' from open_jobs
    union all
    select week_start, 'years', level_years, role_group, metro,
           opportunity_key, employer_id, genai_mention, 'open' from open_jobs
)

select
    r.week_start,
    w.week_complete,
    r.level_rule,
    r.level,
    r.role_group,
    r.metro,
    count(distinct r.opportunity_key) filter (where r.kind = 'new') as new_jobs,
    count(distinct r.employer_id) filter (where r.kind = 'new') as new_job_employers,
    count(distinct r.opportunity_key) filter (where r.kind = 'new' and r.genai_mention)
        as genai_new_jobs,
    count(distinct r.opportunity_key) filter (where r.kind = 'open') as open_jobs_end,
    count(distinct r.opportunity_key) filter (where r.kind = 'open' and r.genai_mention)
        as genai_open_jobs_end
from by_rule r
join week_ends w on w.week_start = r.week_start
group by 1, 2, 3, 4, 5, 6
