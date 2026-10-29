{{ config(materialized='table') }}

-- One row per opportunity per reporting day. An opportunity is a duplicate group (strong or
-- reviewed evidence) counted once, or a posting that is in no group. Possible duplicates
-- (flags) stay separate opportunities. Availability rolls up from the members:
--   observed if any member is observed; otherwise possibly_unavailable if any member is;
--   closed only if every member is closed; assumed_inactive if every member is assumed
--   inactive or closed; not_tracked before availability tracking.
-- Member counts per state keep mixed evidence visible.
select
    coalesce('group:' || duplicate_group_id, 'posting:' || posting_id) as opportunity_key,
    report_day,
    min(as_of) as as_of,
    bool_and(is_complete) as is_complete,
    min(duplicate_group_id) as duplicate_group_id,
    count(*) as source_records,
    bool_or(retained) as retained,
    bool_and(cohort = 'initial_import') as all_initial_import,
    count(*) filter (where availability_state = 'observed') as observed_members,
    count(*) filter (where availability_state = 'possibly_unavailable') as possibly_unavailable_members,
    count(*) filter (where availability_state = 'assumed_inactive') as assumed_inactive_members,
    count(*) filter (where availability_state = 'closed') as closed_members,
    count(*) filter (where availability_state is null) as untracked_members,
    case
        when count(*) filter (where availability_state = 'observed') > 0 then 'observed'
        when count(*) filter (where availability_state = 'possibly_unavailable') > 0
            then 'possibly_unavailable'
        when count(*) filter (where availability_state is null) > 0 then 'not_tracked'
        when count(*) = count(*) filter (where availability_state = 'closed') then 'closed'
        else 'assumed_inactive'
    end as availability_state
from {{ ref('int_posting_days') }}
group by 1, 2
