-- Grain: one row per dimension per value per population per availability state, for
-- postings known today (provisional, as of now).
-- Dimensions: employer, source_system, rule_family (screening rules), role_category (AI),
-- place (AI), work_arrangement (AI), early_career (AI). A posting with several role
-- categories or rule families counts under each, so those totals can overlap and cannot be
-- summed (categories_overlap = true). AI dimensions use 'not_extracted' for postings
-- without an extraction yet; they still count.
with populations as (select unnest(array['all', 'retained']) as population),

base as (
    select cp.*, p.population
    from {{ ref('int_current_postings') }} cp
    cross join populations p
    where p.population = 'all' or cp.retained
),

open_flags as (
    select posting_id from {{ ref('stg_duplicate_flags') }} where flag_status = 'open'
    union
    select related_posting_id from {{ ref('stg_duplicate_flags') }} where flag_status = 'open'
),

values_ as (
    select population, posting_id, opportunity_key, availability_state,
           'employer' as dimension, employer_name as value, false as categories_overlap
    from base
    union all
    select population, posting_id, opportunity_key, availability_state,
           'source_system', source_system, false
    from base
    union all
    select b.population, b.posting_id, b.opportunity_key, b.availability_state,
           'rule_family', coalesce(f, 'none'), true
    from base b
    left join lateral unnest(b.rule_families) as f on true
    union all
    select b.population, b.posting_id, b.opportunity_key, b.availability_state,
           'role_category',
           case when not b.extracted then 'not_extracted' else coalesce(c, 'none') end, true
    from base b
    left join lateral jsonb_array_elements_text(b.role_categories) as c on true
    union all
    select population, posting_id, opportunity_key, availability_state,
           'place', case when extracted then ai_place else 'not_extracted' end, false
    from base
    union all
    select population, posting_id, opportunity_key, availability_state,
           'work_arrangement', case when extracted then work_arrangement else 'not_extracted' end,
           false
    from base
    union all
    select population, posting_id, opportunity_key, availability_state,
           'early_career',
           case when not extracted then 'not_extracted'
                else coalesce(early_career, 'not_asked') end,
           false
    from base
)

select
    v.dimension,
    v.value,
    v.population,
    coalesce(v.availability_state, 'not_tracked') as availability_state,
    bool_or(v.categories_overlap) as categories_overlap,
    count(distinct v.posting_id) as source_records,
    count(distinct v.opportunity_key) as opportunities,
    count(distinct v.posting_id) filter (where v.posting_id in (select posting_id from open_flags))
        as with_open_duplicate_flags
from values_ v
group by 1, 2, 3, 4
