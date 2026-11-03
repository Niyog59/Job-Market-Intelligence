-- Grain: one row per current job (rpt_jobs_current) per generative-AI term it mentions, with
-- the job's filter fields repeated, so Tableau can count jobs per term without splitting the
-- comma-separated genai_terms text. Count jobs with COUNTD(job_key).
select
    j.job_key,
    j.employer_name,
    j.role_group,
    j.metro,
    j.level_ai,
    t.term
from {{ ref('rpt_jobs_current') }} j
join {{ ref('int_current_postings') }} cp on cp.posting_id = j.posting_id
join {{ ref('int_genai_mentions') }} g on g.version_id = cp.version_id
cross join unnest(g.genai_terms) as t(term)
