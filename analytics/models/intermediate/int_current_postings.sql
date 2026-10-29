-- One row per posting known today (today's provisional row of int_posting_days), with its
-- employer, content, screening, and AI-extracted fields. Fields from the AI are null when
-- the posting's current content has no extraction yet (extracted = false); such postings
-- still count in inventory.
select
    pd.posting_id,
    pd.report_day,
    pd.as_of,
    pd.board_id,
    b.employer_id,
    e.employer_name,
    b.source_system,
    b.board_token,
    pd.cohort,
    pd.discovery_day,
    p.first_discovered_at,
    pd.last_observed_at,
    pd.version_id,
    v.version_no,
    v.title,
    v.location_text,
    pd.retained,
    pd.availability_state,
    pd.duplicate_group_id,
    coalesce('group:' || pd.duplicate_group_id, 'posting:' || pd.posting_id) as opportunity_key,
    s.rule_families,
    s.rule_texas_eligibility,
    pd.extraction_result_id is not null as extracted,
    pd.extraction_result_id,
    r.derived ->> 'early_career' as early_career,
    r.derived ->> 'sponsorship' as sponsorship,
    r.derived ->> 'work_arrangement' as work_arrangement,
    r.derived ->> 'place' as ai_place,
    r.normalized -> 'work_authorization' ->> 'status' as work_authorization,
    r.normalized -> 'citizenship' ->> 'status' as citizenship,
    r.normalized -> 'clearance' ->> 'status' as clearance,
    r.normalized -> 'graduation' ->> 'status' as graduation,
    r.normalized -> 'seniority' ->> 'value' as seniority,
    r.normalized -> 'employment_type' ->> 'value' as employment_type,
    r.derived -> 'role_categories' as role_categories,
    r.normalized as extraction
from {{ ref('int_posting_days') }} pd
join {{ ref('int_reporting_days') }} d on d.report_day = pd.report_day and not d.is_complete
join {{ ref('stg_postings') }} p on p.posting_id = pd.posting_id
join {{ ref('stg_boards') }} b on b.board_id = pd.board_id
join {{ ref('stg_employers') }} e on e.employer_id = b.employer_id
left join {{ ref('stg_posting_versions') }} v on v.version_id = pd.version_id
left join {{ ref('stg_screenings') }} s on s.posting_id = pd.posting_id
left join {{ ref('stg_extraction_results') }} r on r.result_id = pd.extraction_result_id
