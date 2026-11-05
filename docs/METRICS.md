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

## Snapshot of 2026-11-04

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
| Automated tests | 598 passed in the `test` job (the two Spark test modules are skipped there); 17 passed in the `spark` job; 25 passed in the `airflow` job; dbt build PASS=172 | pytest and dbt results | CI run 41943829850 | CI |
| Automated tests, local | 597 passed, 1 skipped (the backup round trip, because the local login may not create databases); Spark tests 17 passed with Java 21 | pytest results | `pytest` | Mac, 2026-11-04 |
| dbt models | 46: 18 staging, 10 intermediate, 16 reporting, 2 serving | `.sql` files under `analytics/models/` | repository | repository |
| Hosting cost | about $20.58 a month, estimated | t3.small, 20 GiB gp3 disk, one public IPv4, S3, at the AWS list price of 2026-10-29, before credits | [SIZING.md](../deploy/SIZING.md) | estimate; billed cost not measured |
| Extraction accuracy | not measured | agreement with human-reviewed labels | the 30 evaluation cases have draft labels, not reviewed yet | not available |

Coverage is not accuracy: it says a listing has structured details whose quotes were found
in the posting, not that the details are right.

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
```

The other production numbers come from the public `/reports` page, whose tables are the
published `rpt_extraction_coverage`, `rpt_inventory_daily`, and `rpt_pipeline_health_daily`
models, and from the daily summary email (`jobmarket summary send --print` shows the same
text).
