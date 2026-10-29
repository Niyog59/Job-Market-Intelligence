-- Grain: one row per skill per requirement level per mention kind per population, over
-- postings currently listed (observed or possibly unavailable) whose current content has an
-- extraction. mention_kind 'alternative' means the skill was asked for as one of a set
-- ("Python or Java"): it is not an individual requirement. share_of_extracted divides by
-- extracted_postings, the postings in the population that could have mentioned a skill.
-- A posting asking for many skills counts once per skill, so rows do not sum to a total.
with populations as (select unnest(array['all', 'retained']) as population),

listed as (
    select cp.posting_id, cp.opportunity_key, p.population
    from {{ ref('int_current_postings') }} cp
    cross join populations p
    where cp.extracted
      and cp.availability_state in ('observed', 'possibly_unavailable')
      and (p.population = 'all' or cp.retained)
),

denominators as (
    select population, count(*) as extracted_postings from listed group by 1
)

select
    sm.skill,
    sm.requirement,
    sm.mention_kind,
    l.population,
    count(distinct l.posting_id) as postings,
    count(distinct l.opportunity_key) as opportunities,
    d.extracted_postings,
    round(100.0 * count(distinct l.posting_id) / nullif(d.extracted_postings, 0), 1)
        as share_of_extracted
from {{ ref('int_skill_mentions') }} sm
join listed l on l.posting_id = sm.posting_id
join denominators d on d.population = l.population
group by 1, 2, 3, 4, d.extracted_postings
