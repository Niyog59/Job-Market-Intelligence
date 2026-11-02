-- Grain: one row per week (Monday to Sunday, America/Chicago) per skill per role group: new
-- kept jobs that week (initial imports excluded) that require the skill, individually or as
-- one of a set of choices. Ranks for a bump chart are computed in Tableau (RANK per week).
with new_jobs as (
    select distinct
        date_trunc('week', {{ reporting_day('cp.first_discovered_at') }})::date as week_start,
        coalesce('group:' || cp.duplicate_group_id, 'posting:' || cp.posting_id)
            as opportunity_key,
        cp.posting_id,
        coalesce(a.role_group, 'Not extracted') as role_group
    from {{ ref('int_current_postings') }} cp
    left join {{ ref('int_job_attributes') }} a on a.result_id = cp.extraction_result_id
    where cp.retained and cp.cohort = 'discovered'
)

select
    n.week_start,
    m.skill,
    n.role_group,
    count(distinct n.opportunity_key) as new_jobs_requiring
from new_jobs n
join {{ ref('int_skill_mentions') }} m on m.posting_id = n.posting_id
where m.requirement = 'required'
group by 1, 2, 3
