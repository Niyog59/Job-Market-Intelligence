-- Grain: one row per concept per status per population, over postings currently listed
-- (observed or possibly unavailable). Concepts are kept separate: sponsorship, work
-- authorization, citizenship, clearance. Three denominators, because they answer different
-- questions:
--   share_of_listed: among all listed postings in the population (including ones not yet
--     extracted), e.g. "% of all postings that explicitly offer sponsorship";
--   share_of_extracted: among postings with an extraction;
--   share_of_stating: among postings that state a policy for this concept (statuses other
--     than not_stated and ambiguous); null for the not_stated and ambiguous rows.
-- not_stated and ambiguous are never folded into yes or no.
with populations as (select unnest(array['all', 'retained']) as population),

listed as (
    select cp.*, p.population
    from {{ ref('int_current_postings') }} cp
    cross join populations p
    where cp.availability_state in ('observed', 'possibly_unavailable')
      and (p.population = 'all' or cp.retained)
),

statements as (
    select population, posting_id, extracted, 'sponsorship' as concept, sponsorship as status
    from listed
    union all
    select population, posting_id, extracted, 'work_authorization', work_authorization
    from listed
    union all
    select population, posting_id, extracted, 'citizenship', citizenship from listed
    union all
    select population, posting_id, extracted, 'clearance', clearance from listed
),

labelled as (
    select population, posting_id, concept,
           case when not extracted then 'not_extracted' else status end as status,
           extracted and status not in ('not_stated', 'ambiguous') as states_policy
    from statements
),

totals as (
    select population, concept,
           count(*) as listed_postings,
           count(*) filter (where status <> 'not_extracted') as extracted_postings,
           count(*) filter (where states_policy) as stating_postings
    from labelled group by 1, 2
)

select
    l.concept,
    l.status,
    l.population,
    count(*) as postings,
    t.listed_postings,
    t.extracted_postings,
    t.stating_postings,
    round(100.0 * count(*) / nullif(t.listed_postings, 0), 1) as share_of_listed,
    case when l.status = 'not_extracted' then null
         else round(100.0 * count(*) / nullif(t.extracted_postings, 0), 1) end
        as share_of_extracted,
    case when bool_and(l.states_policy)
         then round(100.0 * count(*) / nullif(t.stating_postings, 0), 1) end
        as share_of_stating
from labelled l
join totals t on t.population = l.population and t.concept = l.concept
group by 1, 2, 3, t.listed_postings, t.extracted_postings, t.stating_postings
