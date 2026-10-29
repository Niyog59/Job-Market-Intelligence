-- Nothing known after a day's as_of instant is attached to that day.
select pd.posting_id, pd.report_day
from {{ ref('int_posting_days') }} pd
left join {{ ref('stg_extraction_results') }} r on r.result_id = pd.extraction_result_id
left join {{ ref('stg_duplicate_group_members') }} m
    on m.group_id = pd.duplicate_group_id and m.posting_id = pd.posting_id
where pd.last_observed_at > pd.as_of
   or pd.availability_state_since > pd.as_of
   or r.created_at > pd.as_of
   or m.added_at > pd.as_of
