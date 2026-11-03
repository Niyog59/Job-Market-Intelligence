-- Grain: one row per distinct job (duplicate postings of the same job merged) that is
-- currently listed (observed or possibly unavailable) and kept by the screening rules. The
-- Tableau dashboards and the homepage numbers start here. Attributes come from the posting
-- seen most recently in the job's duplicate group; discovery from its earliest posting.
with listed as (
    select cp.*
    from {{ ref('int_current_postings') }} cp
    where cp.retained and cp.availability_state in ('observed', 'possibly_unavailable')
),

representative as (
    select distinct on (opportunity_key) *
    from listed
    order by opportunity_key, last_observed_at desc, posting_id desc
),

groups as (
    select
        opportunity_key,
        count(*) as postings_in_group,
        min(first_discovered_at) as first_discovered_at,
        (array_agg(cohort order by first_discovered_at, posting_id))[1] as cohort
    from listed
    group by opportunity_key
)

select
    r.opportunity_key as job_key,
    r.posting_id,
    r.employer_name,
    r.title,
    coalesce(a.metro, 'Not extracted') as metro,
    a.city,
    coalesce(r.work_arrangement, 'not_extracted') as work_arrangement,
    coalesce(r.employment_type, 'not_extracted') as employment_type,
    coalesce(a.role_group, 'Not extracted') as role_group,
    array_to_string(array(select jsonb_array_elements_text(r.role_categories)), ', ')
        as role_categories,
    coalesce(a.level_ai, 'Not extracted') as level_ai,
    coalesce(a.level_years, 'Not extracted') as level_years,
    a.overall_min_years,
    coalesce(a.minimum_degree, 'Not extracted') as minimum_degree,
    r.early_career,
    r.seniority,
    r.sponsorship,
    r.work_authorization,
    r.citizenship,
    r.clearance,
    coalesce(g.genai_mention, false) as genai_mention,
    array_to_string(g.genai_terms, ', ') as genai_terms,
    r.extracted,
    gr.first_discovered_at,
    {{ reporting_day('gr.first_discovered_at') }} as discovery_day,
    date_trunc('week', {{ reporting_day('gr.first_discovered_at') }})::date as discovery_week,
    gr.cohort,
    gr.postings_in_group,
    v.posting_url,
    r.as_of
from representative r
join groups gr on gr.opportunity_key = r.opportunity_key
left join {{ ref('int_job_attributes') }} a on a.result_id = r.extraction_result_id
left join {{ ref('int_genai_mentions') }} g on g.version_id = r.version_id
left join {{ ref('stg_posting_versions') }} v on v.version_id = r.version_id
