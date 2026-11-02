-- Grain: one row per current job (rpt_jobs_current) per skill per requirement level, with the
-- job's filter fields repeated so one Tableau sheet can filter by them. mention_kind
-- "alternative" means one of a set of choices ("Python or Java"), not an individual
-- requirement. Count jobs with COUNTD(job_key) and employers with COUNTD(employer_name).
select
    j.job_key,
    j.employer_name,
    j.role_group,
    j.metro,
    j.level_ai,
    j.level_years,
    m.skill,
    m.requirement,
    m.mention_kind
from {{ ref('rpt_jobs_current') }} j
join {{ ref('int_skill_mentions') }} m on m.posting_id = j.posting_id
