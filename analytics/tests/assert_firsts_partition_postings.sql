-- Every posting is counted exactly once as an initial import or a discovery, on its
-- discovery day; repeated observations add nothing.
with counted as (
    select coalesce(sum(initial_imports + discoveries), 0) as firsts
    from {{ ref('rpt_daily_activity') }} where population = 'all'
),
postings as (select count(*) as n from {{ ref('stg_postings') }})
select * from counted, postings where counted.firsts <> postings.n
