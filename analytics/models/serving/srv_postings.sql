-- One row per listing with stored content (its current version), for the job-search
-- website. A view over staging, so it is always current: new collections and extractions
-- appear without rebuilding analytics.
--
-- Availability is the ingestion's current state (Module 6); the website never derives it.
-- AI fields come from the extraction of the CURRENT version only (null when there is none,
-- extraction_status says why). requested_skills are skills asked for as required or
-- preferred, alone or as one of a set of alternatives; passing mentions are excluded.
-- cohort is the Module 8 rule: initial_import or discovered.
with current_results as (
    select distinct on (r.posting_id) r.*
    from {{ ref('stg_extraction_results') }} r
    join {{ ref('stg_postings') }} p
        on p.posting_id = r.posting_id and r.version_id = p.current_version_id
    order by r.posting_id, r.created_at desc, r.result_id desc
),

current_jobs as (
    select distinct on (j.posting_id) j.posting_id, j.job_status
    from {{ ref('stg_extraction_jobs') }} j
    join {{ ref('stg_postings') }} p
        on p.posting_id = j.posting_id and j.version_id = p.current_version_id
    order by j.posting_id, j.job_id desc
),

groups as (
    select distinct on (m.posting_id) m.posting_id, m.group_id
    from {{ ref('stg_duplicate_group_members') }} m
    join {{ ref('stg_duplicate_groups') }} g on g.group_id = m.group_id
    where g.group_status = 'active'
    order by m.posting_id, m.group_id
),

open_flags as (
    select posting_id, count(*) as open_duplicate_flags
    from (
        select posting_id from {{ ref('stg_duplicate_flags') }} where flag_status = 'open'
        union all
        select related_posting_id from {{ ref('stg_duplicate_flags') }} where flag_status = 'open'
    ) f
    group by posting_id
)

select
    p.posting_id,
    p.board_id,
    b.employer_id,
    b.employer_name,
    b.source_system,
    b.board_token,
    p.source_posting_id,
    v.version_id,
    v.version_no,
    v.title,
    v.location_text,
    v.posting_url,
    v.apply_url,
    v.description_length,
    v.description_omitted,
    v.salary_range,
    v.search_vector,
    p.first_discovered_at,
    p.last_observed_at,
    v.first_seen_at as content_seen_at,
    o.source_published_at,
    o.source_created_at,
    p.current_availability_state as availability_state,
    p.current_qualifying_absences as qualifying_absences,
    s.retained,
    s.needs_review,
    c.cohort,
    g.group_id as duplicate_group_id,
    coalesce('group:' || g.group_id, 'posting:' || p.posting_id) as opportunity_key,
    coalesce(f.open_duplicate_flags, 0) as open_duplicate_flags,
    r.result_id is not null as extracted,
    r.result_id as extraction_result_id,
    r.validation_status,
    case when r.result_id is not null then 'extracted'
         else coalesce(j.job_status, 'not_queued') end as extraction_status,
    case when r.result_id is not null then array(
        select jsonb_array_elements_text(r.derived -> 'role_categories')) end as role_categories,
    r.derived ->> 'early_career' as early_career,
    r.derived ->> 'place' as place,
    r.derived ->> 'work_arrangement' as work_arrangement,
    r.derived ->> 'sponsorship' as sponsorship,
    r.normalized -> 'employment_type' ->> 'value' as employment_type,
    r.normalized -> 'seniority' ->> 'value' as seniority,
    r.normalized -> 'work_authorization' ->> 'status' as work_authorization,
    r.normalized -> 'citizenship' ->> 'status' as citizenship,
    r.normalized -> 'clearance' ->> 'status' as clearance,
    r.normalized -> 'graduation' ->> 'status' as graduation,
    (select min((x ->> 'min_years')::numeric)
       from jsonb_array_elements(r.normalized -> 'experience') x
      where x ->> 'scope' = 'overall' and x ->> 'status' = 'required'
        and x ->> 'min_years' is not null) as overall_min_years,
    case when r.result_id is not null then array(
        select distinct skill from (
            select sk ->> 'canonical' as skill
            from jsonb_array_elements(r.normalized -> 'skills') sk
            where sk ->> 'status' in ('required', 'preferred')
            union all
            select m ->> 'canonical'
            from jsonb_array_elements(r.normalized -> 'skill_groups') sg
            cross join lateral jsonb_array_elements(sg -> 'members') m
            where sg ->> 'status' in ('required', 'preferred')
        ) requested
        where skill is not null
        order by skill) end as requested_skills,
    r.normalized as extraction,
    b.last_complete_collected_at as board_last_complete_at,
    b.last_trusted_check_at as board_last_trusted_check_at,
    b.on_hold as board_on_hold,
    b.hold_opened_at as board_hold_opened_at
from {{ ref('stg_postings') }} p
join {{ ref('stg_posting_versions') }} v on v.version_id = p.current_version_id
join {{ ref('stg_posting_observations') }} o on o.observation_id = p.current_observation_id
join {{ ref('stg_screenings') }} s on s.posting_id = p.posting_id
join {{ ref('srv_boards') }} b on b.board_id = p.board_id
join {{ ref('int_posting_cohorts') }} c on c.posting_id = p.posting_id
left join current_results r on r.posting_id = p.posting_id
left join current_jobs j on j.posting_id = p.posting_id
left join groups g on g.posting_id = p.posting_id
left join open_flags f on f.posting_id = p.posting_id
