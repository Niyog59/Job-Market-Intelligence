-- One row per current posting per skill per requirement level (required, preferred,
-- mentioned, ambiguous). mention_kind says whether the posting asks for the skill on its
-- own ("individual") or as one of a set of alternatives ("alternative": "Python or Java"
-- gives Python and Java an alternative mention each, never two individual requirements).
with skills as (
    select
        cp.posting_id,
        s ->> 'canonical' as skill,
        s ->> 'status' as requirement
    from {{ ref('int_current_postings') }} cp
    cross join lateral jsonb_array_elements(cp.extraction -> 'skills') as s
    where cp.extracted
),

alternatives as (
    select distinct
        cp.posting_id,
        m ->> 'canonical' as skill,
        g ->> 'status' as requirement
    from {{ ref('int_current_postings') }} cp
    cross join lateral jsonb_array_elements(cp.extraction -> 'skill_groups') as g
    cross join lateral jsonb_array_elements(g -> 'members') as m
    where cp.extracted and g ->> 'relation' in ('any_of', 'at_least')
)

select distinct
    s.posting_id,
    s.skill,
    s.requirement,
    case when a.skill is not null then 'alternative' else 'individual' end as mention_kind
from skills s
left join alternatives a
    on a.posting_id = s.posting_id and a.skill = s.skill and a.requirement = s.requirement
