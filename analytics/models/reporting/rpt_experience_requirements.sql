-- Grain: one row per measure per value per population, over postings currently listed
-- (observed or possibly unavailable). Measures: early_career, overall_min_years (the
-- smallest required overall minimum, bucketed), graduation (explicit graduation
-- requirement), seniority. listed_postings counts every listed posting in the population;
-- extracted_postings only those with an extraction (the denominator for these measures);
-- not_extracted rows show the gap.
with populations as (select unnest(array['all', 'retained']) as population),

listed as (
    select cp.*, p.population,
        (select min((x ->> 'min_years')::numeric)
           from jsonb_array_elements(cp.extraction -> 'experience') as x
          where x ->> 'scope' = 'overall' and x ->> 'status' = 'required'
            and x ->> 'min_years' is not null) as overall_min_years
    from {{ ref('int_current_postings') }} cp
    cross join populations p
    where cp.availability_state in ('observed', 'possibly_unavailable')
      and (p.population = 'all' or cp.retained)
),

measures as (
    select population, posting_id, extracted, 'early_career' as measure,
           case when not extracted then 'not_extracted'
                else coalesce(early_career, 'not_asked') end as value
    from listed
    union all
    select population, posting_id, extracted, 'overall_min_years',
           case when not extracted then 'not_extracted'
                when overall_min_years is null then 'not_stated'
                when overall_min_years < 1 then '0'
                when overall_min_years < 2 then '1'
                when overall_min_years < 3 then '2'
                when overall_min_years < 5 then '3-4'
                when overall_min_years < 8 then '5-7'
                else '8+' end
    from listed
    union all
    select population, posting_id, extracted, 'graduation',
           case when extracted then graduation else 'not_extracted' end
    from listed
    union all
    select population, posting_id, extracted, 'seniority',
           case when extracted then seniority else 'not_extracted' end
    from listed
),

totals as (
    select population, count(*) as listed_postings,
           count(*) filter (where extracted) as extracted_postings
    from listed group by 1
)

select
    m.measure,
    m.value,
    m.population,
    count(*) as postings,
    t.listed_postings,
    t.extracted_postings,
    case when m.value = 'not_extracted' then null
         else round(100.0 * count(*) / nullif(t.extracted_postings, 0), 1) end
        as share_of_extracted
from measures m
join totals t on t.population = m.population
group by 1, 2, 3, t.listed_postings, t.extracted_postings
