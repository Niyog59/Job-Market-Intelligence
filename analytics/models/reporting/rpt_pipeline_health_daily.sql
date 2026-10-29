-- Grain: one row per reporting day. Scans by outcome (a scan counts on the day it started),
-- boards scanned, boards on a mass-disappearance hold at the day's end, AI extraction
-- requests and recorded token usage that day (reporting time zone; the provider's daily
-- quota uses Pacific days, see extraction status), and extraction coverage of listed
-- retained postings at the day's end.
with days as (select * from {{ ref('int_reporting_days') }}),

scans as (
    select {{ reporting_day('started_at') }} as report_day,
           count(*) as scans,
           count(*) filter (where scan_status = 'complete') as complete_scans,
           count(*) filter (where scan_status = 'partial') as partial_scans,
           count(*) filter (where scan_status = 'failed') as failed_scans,
           count(distinct board_id) as boards_scanned
    from {{ ref('stg_board_scans') }}
    group by 1
),

attempts as (
    select {{ reporting_day('started_at') }} as report_day,
           count(*) as extraction_requests,
           count(*) filter (where attempt_outcome = 'success') as extraction_successes,
           count(*) filter (where attempt_outcome <> 'success') as extraction_failures,
           coalesce(sum(prompt_tokens), 0) as prompt_tokens,
           coalesce(sum(output_tokens), 0) + coalesce(sum(thought_tokens), 0) as output_tokens
    from {{ ref('stg_extraction_attempts') }}
    group by 1
),

coverage as (
    select report_day,
           count(*) as listed_retained_postings,
           count(*) filter (where extraction_result_id is not null) as extracted
    from {{ ref('int_posting_days') }}
    where retained and availability_state in ('observed', 'possibly_unavailable')
    group by 1
)

select
    d.report_day,
    d.is_complete,
    coalesce(s.scans, 0) as scans,
    coalesce(s.complete_scans, 0) as complete_scans,
    coalesce(s.partial_scans, 0) as partial_scans,
    coalesce(s.failed_scans, 0) as failed_scans,
    coalesce(s.boards_scanned, 0) as boards_scanned,
    (select count(*) from {{ ref('stg_board_holds') }} h
      where h.opened_at <= d.as_of and (h.released_at is null or h.released_at > d.as_of))
        as boards_on_hold,
    coalesce(a.extraction_requests, 0) as extraction_requests,
    coalesce(a.extraction_successes, 0) as extraction_successes,
    coalesce(a.extraction_failures, 0) as extraction_failures,
    coalesce(a.prompt_tokens, 0) as prompt_tokens,
    coalesce(a.output_tokens, 0) as output_tokens,
    coalesce(c.listed_retained_postings, 0) as listed_retained_postings,
    coalesce(c.extracted, 0) as listed_retained_extracted,
    round(100.0 * c.extracted / nullif(c.listed_retained_postings, 0), 1)
        as retained_extraction_coverage_pct
from days d
left join scans s on s.report_day = d.report_day
left join attempts a on a.report_day = d.report_day
left join coverage c on c.report_day = d.report_day
