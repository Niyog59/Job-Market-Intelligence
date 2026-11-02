-- Grain: one row per pair of skills (skill_a sorts before skill_b) per role group: how many
-- current jobs ask for both, as a requirement or a preference. Jobs add up across role groups
-- (each job has one group); employers do not.
with skills as (
    select distinct job_key, employer_name, role_group, skill
    from {{ ref('rpt_job_skills') }}
    where requirement in ('required', 'preferred')
)

select
    a.skill as skill_a,
    b.skill as skill_b,
    a.role_group,
    count(distinct a.job_key) as jobs,
    count(distinct a.employer_name) as employers
from skills a
join skills b on b.job_key = a.job_key and a.skill < b.skill
group by 1, 2, 3
