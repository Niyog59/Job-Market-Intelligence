-- One row per AI extraction result: the grouped fields the dashboards use.
--   role_group: one group per job, by priority Data > AI/ML > Software > Other (decision
--     2026-11-02): Data = data analysis, BI, data and analytics engineering; AI/ML = ML and
--     data science; Software = software engineering, backend, full stack.
--   level_ai: Junior (early career, entry, internship) / Mid / Senior+ (senior and above) /
--     Unclear, from the AI's seniority and early-career answers.
--   level_years: by the smallest required overall minimum years: 0-2 / 3-4 / 5+ / Not stated.
--   minimum_degree: the lowest degree listed as required (the entry bar), "Degree preferred
--     only", or "Not stated".
--   metro and city: where the job is, from the AI's place and first location.
with base as (
    select
        r.result_id,
        r.derived -> 'role_categories' as roles,
        r.derived ->> 'early_career' as early_career,
        r.normalized -> 'seniority' ->> 'value' as seniority,
        r.derived ->> 'place' as place,
        r.normalized -> 'locations' -> 0 ->> 'city' as city,
        (select min((x ->> 'min_years')::numeric)
           from jsonb_array_elements(r.normalized -> 'experience') as x
          where x ->> 'scope' = 'overall' and x ->> 'status' = 'required'
            and x ->> 'min_years' is not null) as overall_min_years,
        (select min(case e ->> 'level'
                        when 'high_school' then 1 when 'associate' then 2
                        when 'bachelor' then 3 when 'master' then 4 when 'doctorate' then 5
                    end)
           from jsonb_array_elements(r.normalized -> 'education') as e
          where e ->> 'status' = 'required') as min_required_degree_rank,
        exists (select 1 from jsonb_array_elements(r.normalized -> 'education') as e
                 where e ->> 'status' = 'preferred') as degree_preferred,
        exists (select 1 from jsonb_array_elements(r.normalized -> 'education') as e
                 where e ->> 'status' = 'required'
                   and e ->> 'level' in ('certificate', 'other')) as other_required
    from {{ ref('stg_extraction_results') }} r
)

select
    result_id,
    case
        when roles ?| array['data_analysis', 'bi', 'data_engineering', 'analytics_engineering']
            then 'Data'
        when roles ?| array['ml', 'data_science'] then 'AI/ML'
        when roles ?| array['software_engineering', 'backend', 'full_stack'] then 'Software'
        else 'Other'
    end as role_group,
    case
        when early_career in ('yes', 'internship') or seniority in ('entry', 'intern')
            then 'Junior'
        when seniority = 'mid' then 'Mid'
        when seniority in ('senior', 'staff', 'lead', 'principal', 'manager', 'director',
                           'executive') then 'Senior+'
        else 'Unclear'
    end as level_ai,
    overall_min_years,
    case
        when overall_min_years is null then 'Not stated'
        when overall_min_years < 3 then '0-2 years'
        when overall_min_years < 5 then '3-4 years'
        else '5+ years'
    end as level_years,
    case min_required_degree_rank
        when 1 then 'High school'
        when 2 then 'Associate'
        when 3 then 'Bachelor'
        when 4 then 'Master'
        when 5 then 'Doctorate'
        else case
            when other_required then 'Other credential'
            when degree_preferred then 'Degree preferred only'
            else 'Not stated'
        end
    end as minimum_degree,
    case place
        when 'local_dfw' then 'DFW'
        when 'local_houston' then 'Houston'
        when 'local_both' then 'DFW and Houston'
        when 'remote_texas' then 'Remote (Texas)'
        when 'remote_ambiguous' then 'Remote (unclear)'
        else 'Elsewhere'
    end as metro,
    city
from base
