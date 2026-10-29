{{ config(materialized='table') }}

-- One row per posting per reporting day, from the day it was first discovered. Every column
-- is what was known at that day's as_of instant (end of day, or now for today):
--   version / last observation: the latest sighting at or before as_of;
--   retained: the screening stored with that version (what we decided when we saw it);
--   availability_state: the latest availability event at or before as_of (null before
--     availability tracking started: not reconstructed);
--   extraction: the latest AI result for that version created at or before as_of;
--   group: a duplicate group the posting belonged to at as_of.
-- So a later closure, extraction, or grouping never rewrites an earlier day.
with posting_days as (
    select
        c.posting_id,
        c.board_id,
        c.cohort,
        c.discovery_day,
        d.report_day,
        d.as_of,
        d.is_complete
    from {{ ref('int_posting_cohorts') }} c
    join {{ ref('int_reporting_days') }} d on c.first_discovered_at <= d.as_of
)

select
    pd.posting_id,
    pd.report_day,
    pd.as_of,
    pd.is_complete,
    pd.board_id,
    pd.cohort,
    pd.discovery_day,
    obs.version_id,
    obs.observed_at as last_observed_at,
    {{ reporting_day('obs.observed_at') }} = pd.report_day as observed_this_day,
    coalesce(v.retained_when_recorded, false) as retained,
    ev.to_state as availability_state,
    ev.occurred_at as availability_state_since,
    ex.result_id as extraction_result_id,
    ex.renormalized_at is not null and ex.renormalized_at > pd.as_of
        as extraction_cleaned_up_later,
    grp.group_id as duplicate_group_id
from posting_days pd
left join lateral (
    select o.observation_id, o.observed_at, o.version_id
    from {{ ref('stg_posting_observations') }} o
    where o.posting_id = pd.posting_id and o.observed_at <= pd.as_of
    order by o.observed_at desc, o.board_scan_id desc
    limit 1
) obs on true
left join {{ ref('stg_posting_versions') }} v on v.version_id = obs.version_id
left join lateral (
    select e.to_state, e.occurred_at
    from {{ ref('stg_availability_events') }} e
    where e.posting_id = pd.posting_id and e.occurred_at <= pd.as_of
    order by e.occurred_at desc, e.event_id desc
    limit 1
) ev on true
left join lateral (
    select r.result_id, r.renormalized_at
    from {{ ref('stg_extraction_results') }} r
    where r.version_id = obs.version_id and r.created_at <= pd.as_of
    order by r.created_at desc, r.result_id desc
    limit 1
) ex on true
left join lateral (
    select m.group_id
    from {{ ref('stg_duplicate_group_members') }} m
    join {{ ref('stg_duplicate_groups') }} g on g.group_id = m.group_id
    where m.posting_id = pd.posting_id
      and m.added_at <= pd.as_of
      and g.created_at <= pd.as_of
      and (g.dissolved_at is null or g.dissolved_at > pd.as_of)
    order by m.group_id
    limit 1
) grp on true
