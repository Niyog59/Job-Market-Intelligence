# Project metrics

Measured values, each with its definition, where it came from, when, and in which
environment. Back to the [README](../README.md).

Environments:

- **Production**: the server behind https://jobobservatory.dev, read through its public
  pages (`/reports`, `/`) and the daily summary email. The database itself was not queried
  for this page.
- **Mac copy**: my local database, the same data the production server was restored from on
  2026-11-04, as it stood on 2026-11-03.
- **CI**: GitHub Actions on commit `d21bc89` (run 41943829850, 2026-11-05 UTC).

## Historical snapshot: 2026-11-04

These values were measured on 2026-11-04 and are not updated. Current job counts, coverage,
and pipeline health are on the live [Reports page](https://jobobservatory.dev/reports).

| Metric | Value | Definition | Source | Environment |
|---|---|---|---|---|
| Enabled employer boards | 51 (21 Greenhouse, 30 Lever) | boards with `enabled = true` in the registry | `config/greenhouse_boards.toml`, `config/lever_boards.toml` | repository |
| Kept postings, listed | 166 (165 observed, 1 possibly unavailable) | source records kept by the screening rules and still listed | `/reports`, "Current inventory", reports built 21:03 CST | production |
| Kept opportunities, listed | 147 (146 observed, 1 possibly unavailable) | the same, with each duplicate group counted once | `/reports`, "Opportunities" column | production |
| All collected postings, listed | 5,345 (5,271 observed, 74 possibly unavailable) | every listing collected from the 51 boards, kept or not | `/reports?population=all` | production |
| Kept-job extraction coverage | 165 of 166 (99.4%); 1 waiting | listed kept postings with structured details, of all listed kept postings | `/reports`, "Structured details coverage" | production |
| All-listing extraction coverage | 485 of 5,345 (9.1%); 4,855 waiting, 4 to be retried, 1 skipped | the same over every listing | `/reports?population=all` | production |
| Oldest pending extraction | 2026-10-28 (5,202 pending) | `created_at` of the oldest `pending` job in the queue | query 1 below | Mac copy, 2026-11-03; production not measured |
| AI requests | 124 on the Pacific day 2026-11-04, 34 of them failed | requests sent to Gemini and their failures | `/reports`, "Pipeline health"; daily summary | production; the failures have not been looked into yet |
| Collection duration | 133 s for all 51 boards | `finished_at - started_at` of the latest collection run | query 2 below | Mac copy, 2026-11-03 |
| Report publication duration | 25 s (build, tests, statistics, swap) | `report_publish` step duration | query 3 below | Mac copy, 2026-11-03 |
| The 21:00 scheduled run, 2026-11-04 | collection, backup, publication, Sheet update, and summary all finished by 21:04 CST; collection was partial because one board returned HTTP 500 and was queued for retry | the run's steps and results | daily summary email | production |
| Scheduled-run success over a period | not available yet | share of scheduled runs whose steps succeeded | needs at least a week of production runs | production started 2026-11-04 |
| Backup freshness | backup `20261105T030322Z`, 19,690,966 bytes, confirmed in S3 at 21:03 CST on 2026-11-04 | the newest backup with S3 confirmation | daily summary email | production |
| Restore verification | the production database was created on 2026-11-04 by restoring the Mac's backup `20261104T050246Z` (checksums verified, one transaction per part); CI restores the pipeline's own backup on every run | a backup restored into an empty database | `jobmarket backup restore`; CI `deploy` job | production and CI |
| Automated tests | 598 passed in the `test` job (the two Spark test modules are skipped there); 17 passed in the `spark` job; 25 passed in the `airflow` job; `dbt build` passed every node (46 models, 125 data tests, and one run hook) | pytest and dbt results | CI run 41943829850 | CI |
| Automated tests, local | 597 passed, 1 skipped (the backup round trip, because the local login may not create databases); Spark tests 17 passed with Java 21 | pytest results | `pytest` | Mac, 2026-11-04 |
| dbt models | 46: 18 staging, 10 intermediate, 16 reporting, 2 serving | `.sql` files under `analytics/models/` | repository | repository |
| dbt data tests | 125 | tests defined on models and sources | `dbt` manifest | repository |
| Hosting cost | about $20.58 a month, estimated | t3.small, 20 GiB gp3 disk, one public IPv4, S3, at the AWS list price of 2026-10-29, before credits | [SIZING.md](../deploy/SIZING.md) | estimate; billed cost not measured |
| Extraction accuracy | not measured | agreement with human-reviewed labels | the 30 evaluation cases have draft labels, not reviewed yet | not available |

Coverage is not accuracy. It counts listings that have an extraction result, including
results kept with warnings because some quotes were not found in the posting. It does not
say the details are right.

## Queries

Run as the database owner (`docker compose run --rm jobs jobmarket ...` has no SQL shell, so
use `docker compose exec db psql -U postgres -d Job_Market` on the server, or `psql` locally).

```sql
-- 1. Oldest pending extraction and the queue size
SELECT min(created_at) AS oldest_pending, count(*) AS pending
FROM extraction_jobs WHERE status = 'pending';

-- 2. Duration of recent collection runs
SELECT run_id, trigger, status, started_at,
       round(extract(epoch FROM finished_at - started_at)) AS seconds
FROM collection_runs ORDER BY run_id DESC LIMIT 10;

-- 3. Duration of recent report publications
SELECT status, started_at, round(extract(epoch FROM finished_at - started_at)) AS seconds
FROM pipeline_step_runs WHERE step = 'report_publish' ORDER BY started_at DESC LIMIT 10;

-- 4. Scheduled runs and their result over the last 7 days
SELECT status, count(*) FROM collection_runs
WHERE trigger = 'scheduled' AND started_at > now() - interval '7 days'
GROUP BY status;

-- 5. Extraction queue: open jobs and the oldest one, by priority
--    (1 new kept, 2 kept backlog, 3 new other, 4 other backlog)
SELECT priority, status, count(*), min(created_at) AS oldest
FROM extraction_jobs WHERE status IN ('pending', 'running', 'failed')
GROUP BY 1, 2 ORDER BY 1, 2;

-- 6. Extraction runs: how late each started, how long it took, and why it stopped
SELECT run_id, started_at, round(extract(epoch FROM finished_at - started_at)) AS seconds,
       requests, succeeded, failed, stop_reason
FROM extraction_runs ORDER BY run_id DESC LIMIT 24;

-- 7. Requests per Pacific day by outcome, and request duration
SELECT (started_at AT TIME ZONE 'America/Los_Angeles')::date AS pacific_day, outcome,
       count(*), round(avg(extract(epoch FROM finished_at - started_at))::numeric, 1) AS avg_s,
       round(avg(total_tokens)) AS avg_tokens
FROM extraction_attempts GROUP BY 1, 2 ORDER BY 1, 2;

-- 8. Time from queueing and from collection to a result, new listings, last 7 days
SELECT j.priority, count(*),
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM r.created_at - j.created_at)) / 60)::numeric, 1) AS queued_p50_min,
       round((percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM r.created_at - j.created_at)) / 60)::numeric, 1) AS queued_p95_min,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM r.created_at - v.first_seen_at)) / 60)::numeric, 1) AS collected_p50_min
FROM extraction_results r JOIN extraction_jobs j ON j.job_id = r.job_id
JOIN posting_versions v ON v.version_id = r.version_id
WHERE j.priority IN (1, 3) AND r.reused_from_result_id IS NULL
  AND r.created_at > now() - interval '7 days'
GROUP BY 1 ORDER BY 1;
```

Queries 5 to 8 are the baseline for the extraction worker: run them before starting it and
again a few days later. `jobmarket extract status` shows the same numbers for the last 24
hours.

The other production numbers come from the public `/reports` page, whose tables are the
published `rpt_extraction_coverage`, `rpt_inventory_daily`, and `rpt_pipeline_health_daily`
models, and from the daily summary email (`jobmarket summary send --print` shows the same
text).
