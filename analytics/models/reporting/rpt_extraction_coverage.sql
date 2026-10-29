-- Grain: one row per population ('all' or 'retained') for postings currently listed
-- (observed or possibly unavailable): how many have an AI extraction of their current
-- content, and where the rest are in the queue. Postings without an extraction still count
-- in every inventory; only metrics that need extracted fields leave them out.
with populations as (select unnest(array['all', 'retained']) as population),

listed as (
    select cp.posting_id, cp.version_id, cp.extracted, p.population
    from {{ ref('int_current_postings') }} cp
    cross join populations p
    where cp.availability_state in ('observed', 'possibly_unavailable')
      and (p.population = 'all' or cp.retained)
),

latest_jobs as (
    select distinct on (version_id) version_id, job_status
    from {{ ref('stg_extraction_jobs') }}
    order by version_id, created_at desc, job_id desc
)

select
    l.population,
    count(*) as listed_postings,
    count(*) filter (where l.extracted) as extracted,
    count(*) filter (where not l.extracted and j.job_status in ('pending', 'running'))
        as waiting_in_queue,
    count(*) filter (where not l.extracted and j.job_status = 'failed') as failed_will_retry,
    count(*) filter (where not l.extracted and j.job_status = 'gave_up') as gave_up,
    count(*) filter (where not l.extracted and j.job_status = 'skipped') as skipped,
    count(*) filter (where not l.extracted and (j.job_status is null
        or j.job_status in ('succeeded', 'superseded'))) as not_queued_for_current_content,
    round(100.0 * count(*) filter (where l.extracted) / nullif(count(*), 0), 1)
        as coverage_pct
from listed l
left join latest_jobs j on j.version_id = l.version_id
group by 1
