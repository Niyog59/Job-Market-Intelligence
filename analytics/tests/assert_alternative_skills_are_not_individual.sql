-- Every member of an "any of" / "at least" group is reported as an alternative mention,
-- never as an individual requirement.
select cp.posting_id, m ->> 'canonical' as skill
from {{ ref('int_current_postings') }} cp
cross join lateral jsonb_array_elements(cp.extraction -> 'skill_groups') g
cross join lateral jsonb_array_elements(g -> 'members') m
join {{ ref('int_skill_mentions') }} sm
    on sm.posting_id = cp.posting_id and sm.skill = m ->> 'canonical'
   and sm.requirement = g ->> 'status'
where cp.extracted and g ->> 'relation' in ('any_of', 'at_least')
  and sm.mention_kind <> 'alternative'
