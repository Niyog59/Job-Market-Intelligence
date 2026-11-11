# Decisions

The main choices behind the project: the problem, the options I considered, what I chose,
what it costs, and what would make me revisit it. Back to the [README](../README.md).

## Official ATS job board APIs as the only source

- **Problem.** I needed job listings from DFW and Houston employers, complete enough to tell
  when a job disappears.
- **Options.** Scraping career pages; job aggregators; the public job board APIs that
  Greenhouse and Lever provide for each employer's board.
- **Choice.** The public APIs only, one board per employer, enabled only after I verified
  that the employer's own careers page links to that board.
- **Benefit.** The data is structured, permitted, and complete per board, so "the job is not
  in a complete scan" means something. No login walls or bot workarounds.
- **Cost.** Coverage is limited to employers on these two systems, and verifying each board
  takes time (63 enabled boards so far, with the rejected candidates and their reasons kept
  in the registry).
- **Revisit when** important employers turn out to use another system (Ashby, Workday) often
  enough to justify a third connector.

## Keep every version and the raw responses

- **Problem.** Listings change and disappear. I wanted to know what a job said and when,
  not just what it says now.
- **Options.** Keep only the current listing; keep a copy per scan; keep identities,
  meaningful versions, and observations separately, plus the raw pages.
- **Choice.** Identities, versions only for meaningful changes, one observation per sighting,
  and every raw page saved with its checksum ([data model](ARCHITECTURE.md#identity-versions-and-observations)).
- **Benefit.** History stays small but complete, and the reports can rebuild what was known
  on any past day. A recent scan can be replayed under its original time after a rule change,
  while its raw pages are still on the server.
- **Cost.** More tables and more care with ordering: an older response processed late must
  never overwrite newer state, which needed its own tests
  ([`test_history.py`](../tests/test_history.py)). Raw pages cost storage, so they are kept
  4 days on the server and 30 days in S3.
- **Revisit when** raw storage grows past what the retention covers, or replay is never used.

## Conservative availability and duplicate rules

- **Problem.** A job that vanished from one scan might be closed, or the scan might have
  gone wrong. The same opening is sometimes posted twice.
- **Options.** Hide a job the first time it is missing; infer closure from absence; merge
  duplicates by fuzzy title matching.
- **Choice.** Only complete scans count. A job is possibly unavailable after one missing
  complete scan and assumed inactive after three. Closed needs explicit evidence. A board
  where many jobs vanish at once is put on hold instead. Duplicates are grouped only on
  strong evidence (requisition ID or the same job-specific application URL); weaker
  similarity is flagged for review. Nothing is deleted.
- **Benefit.** One broken scan cannot hide real jobs, and a wrong duplicate merge cannot
  hide one either.
- **Cost.** Closed jobs stay visible longer, and some true duplicates stay separate until
  reviewed.
- **Revisit when** checks of job pages (`jobmarket check-pages`) show many assumed-active
  jobs are actually closed.

## Rule-based screening, with AI as enrichment

- **Problem.** I wanted structured requirements (skills, years, degree, level) from free
  text, without letting a model decide which jobs exist in the product.
- **Options.** Rules only; a model decides everything; rules decide retention and a model
  extracts requirements.
- **Choice.** Rules decide which jobs are kept. Gemini (free tier) extracts requirements into
  a fixed JSON schema. Python validates every answer and searches for every quoted piece of
  evidence in the text that was sent. An answer whose quotes are not found is kept with a
  warning, and disagreements with the rules are listed for review.
- **Benefit.** Retention is deterministic and explainable; requirement data comes with its
  evidence; the free tier costs nothing.
- **Cost.** Free-tier limits mean a large backlog of non-kept listings, processed after kept
  ones. Accuracy is not measured yet: 30 labelled cases exist, but their labels have not been
  reviewed by a person, so they measure agreement, not accuracy.
- **Revisit when** the reviewed evaluation gives a real accuracy number, or the backlog stops
  shrinking.

## PostgreSQL and dbt for analytics

- **Problem.** Reports and dashboards need tested tables built from the history.
- **Options.** pandas scripts; SQL models in dbt on PostgreSQL; Spark or Databricks.
- **Choice.** dbt on the same PostgreSQL database, built by a login that can read the
  ingestion tables but write only its own analytics schemas.
- **Benefit.** The data already lives in PostgreSQL, the website reads the same tables, and
  dbt's tests run on every build. One engine to run, back up, and secure.
- **Cost.** Single-machine scale.
- **The Spark experiment.** I rebuilt three reporting models in PySpark
  (`rpt_skill_demand`, `rpt_jobs_current`, `rpt_weekly_jobs`) and checked them against dbt's
  output row for row: same columns, same rows, exact values. They matched in CI on synthetic
  data, on my Mac with the real data at 1x, 10x, and 100x its size (66,367 to 6.6 million input
  rows), and on Databricks Free Edition at 1x. Timings, medians of 5 runs on an M1 Mac, counted
  separately:

  | Measured part | 1x | 100x |
  |---|---|---|
  | PostgreSQL, the three models' SQL on prepared inputs | 0.04 s | 5.18 s |
  | Spark local mode, the three models alone | 4.62 s | 12.40 s |
  | Spark session startup | 3.78 s | 3.37 s |
  | Export to Parquet (only the Spark path needs it) | 0.21 s | 20.95 s |

  These are transformation times on inputs prepared in advance, not the pipeline's runtime.
  At 1x, a full `dbt run` of the same three models took 11.6 s, almost all of it upstream:
  two of the models read a keyword-matching model over every description. On Databricks Free
  Edition the three models took 10.9 s at 1x. PostgreSQL was faster at every size I measured,
  so I stayed with dbt. The full method is in [docs/spark/README.md](spark/README.md).
- **Revisit when** the data grows well past 100 times today's size, or moves to a store Spark
  reads natively. dbt has Databricks and Snowflake adapters, so the models could move as SQL.

## Airflow for scheduling

- **Problem.** Collection, retries, extraction, publication, backups, and the daily summary
  have to run on time and in order, and failures have to be visible.
- **Options.** cron; systemd timers; Airflow.
- **Choice.** Airflow 3 with three DAGs, one task at a time, every task an ordinary
  `jobmarket` command. Retry state lives in the database, not in Airflow.
- **Benefit.** Run history, a UI, dependencies between steps (a failed collection still
  publishes and backs up), and the time zone handled for daylight saving.
- **Cost.** Airflow is the largest memory user on the server (its scheduler peaked at 640 MB in
  the pre-deployment measurement), it is the reason the server needed 2 GiB, and it adds its
  own database and upgrades. cron would have worked at this size.
- **Revisit when** the schedule stays this simple: cron plus the existing commands would be
  enough, and the server could shrink.

## A separate extraction worker

- **Problem.** New listings waited for the next hourly batch (up to an hour, or longer when
  more than 20 arrived at once) before their requirements were extracted, while the
  per-minute limit sat unused for most of each hour.
- **Options.** Run the hourly batch more often; a long-running worker on the existing
  PostgreSQL queue; a separate queue system (Redis, Kafka).
- **Choice.** One worker service beside Airflow, on the same queue. Collection queues new
  content after every board; the worker takes it as soon as it is free, in two lanes: new
  listings first, the backlog with spare requests and never more than the safety stop
  minus a daily reserve of 200 for new listings. The hourly batch stays as the fallback and
  skips itself while the worker runs.
- **Benefit.** New listings are extracted within minutes of collection while the day's
  requests last, without a new system to run: claims, pacing, and the daily count live in
  the database and hold across the worker, the batch, and manual runs. One request at a
  time already uses the per-minute limit (one request every 4 seconds; a request took 3.6
  seconds at the median on the Mac copy), so no concurrency was added.
- **Cost.** One more always-on process (256 MB limit) on a 2 GiB server, and more queue
  logic: claims, recovery after a crash, scheduled retries. It does not raise daily
  throughput: the free daily limit (about 450 usable requests) still decides how fast the
  backlog shrinks, now at most 250 a day.
- **Revisit when** the daily limit changes, new listings regularly exceed the reserve, or
  the server's memory measurement shows the worker does not fit.

## Show new listings before extraction

- **Problem.** On 2026-11-06 the default search on the live site showed 13 jobs. The data
  was current (the serving views are live views), but the defaults hid most of it: kept
  jobs only (147 of 5,297 active opportunities) and early career only (13 of those 147).
  The early-career filter also hides a kept job as soon as the AI says it is not early
  career. Collection ran twice a day, so a job could wait up to 15 hours to be found.
- **Options.** Change the search defaults; a separate list with no AI filters; make that
  list the home page.
- **Choice (Niyog, 2026-11-06).** A separate public page, `/newest`, linked from the search
  page and the header. It lists jobs first discovered in the past 3 days, kept jobs at every
  level by default, newest first, with only the rule-based screening as filters. Initial
  imports are hidden behind a checkbox. The search kept its defaults until 2026-11-07, when
  Niyog changed its career level to all levels: the early-career default had left 12 of 146
  kept jobs on the homepage, because the AI had read all of them and marked 134 mid-level or
  senior.
- **Also chosen.** Both lists hide jobs dated more than 30 days ago (source date, else
  discovery date) unless asked. Nothing is deleted.
- **Benefit.** A new job can be opened and applied to within seconds of its collection,
  whatever the AI queue is doing.
- **Cost.** The rule-based screening is the only relevance check on that page, and it has
  known misses (it keeps some roles outside the target families and skips some inside). The
  30-day rule hides most jobs on older boards: on the Mac copy, 134 of 156 kept active jobs
  were dated more than 30 days before 2026-11-06.
- **Revisit when** collection runs more often (the 3-day window could shrink) or the
  screening rules change.

## Three collections a day

- **Problem.** With collections at noon and 9 PM, a job posted in the evening waited until
  noon the next day: 15 hours. The request was to check every 15 to 30 minutes.
- **Options.** A full collection every 15 to 30 minutes; a cheap check of each board's list
  of job IDs, with a full collection of a board only when a new ID appears; one more full
  collection a day.
- **Measured.** A full collection of the 51 boards took 68 to 158 seconds on the Mac and
  saved about 88 MB of raw responses (30 Lever boards about 71 MB, 21 Greenhouse boards
  about 17 MB) and about 5,200 observation rows (run 9, 2026-11-03). Every 30 minutes, the
  raw files alone would be about 4.2 GB a day against a 20 GiB disk with 4 days of
  retention. Absences are counted per complete scan, so 3 missing scans would mark a
  removed job inactive within about an hour.
- **Choice (Niyog, 2026-11-06).** One more full collection, at 8 AM, so 8:00, 12:00, and
  21:00. The ID check was dropped. Backups, pruning, the summary, and Airflow cleanup stay
  after the 21:00 run; reports and the Sheet are rebuilt after every collection.
- **Benefit.** The longest wait for a new job drops from 15 hours (9 PM to noon) to 11
  (9 PM to 8 AM), and a job posted early in the morning is found by 8 AM or noon.
- **Cost.** About 50% more raw storage: roughly 1.05 GB on the server instead of 0.7, and
  about 2.6 GB more in S3 over its 30 days (cents a month). One more report rebuild and
  Sheet update a day. Removed jobs reach "assumed inactive" after about 1 day instead of
  about 1.5 (accepted by Niyog), so a board that drops a job by mistake for a day hides it
  sooner; mass disappearances still put the board on hold.
- **Revisit when** more boards make a collection take much longer, or new jobs posted in
  the evening matter more than the overnight gap.

## Adding employers in measured batches

- **Problem.** 51 boards are a small sample, and the request was to cover as many verified
  employers in DFW, Houston, and remote-for-Texas as the system can reliably handle.
- **Choice (Niyog, 2026-11-06).** Batches of at most 25 boards, each verified (see
  [Adding boards](COLLECTION.md#adding-boards)) and measured before the next. A new
  employer's existing listings are initial imports: kept ones go to the AI's new lane, the
  rest to the backlog.
- **Batch 1 (2026-11-06).** 12 boards: 10 of the registry's pending boards whose official
  link could now be shown, and 2 new ones (Fervo Energy, Toyota Connected). On the Mac copy
  their first collection took 17 seconds and 17.0 MB of raw responses for 1,110 listings,
  39 of them kept. A full collection grows from about 88 MB to about 105 MB.
- **Deferred, with reasons.** Cribl (remote jobs only, no DFW or Houston evidence), Field AI
  (no Texas jobs), Kodiak and Later (official links verified, no DFW or Houston jobs now),
  and DRW, Bluestaq, CoreWeave, Horace Mann, and Bounteous (no link from the careers page to
  the board was found). Turing, Vailexa, Electronic Power Design, Innovative Signal
  Analysis, Upwing Energy, and Gopuff were not rechecked in this batch.
- **Cost.** Each batch adds raw storage, collection time, and AI backlog. The AI's free tier,
  not collection, limits how much of the collected data gets structured details.
- **Revisit when** a full collection takes more than about 10 minutes, the server's memory
  or disk measurements tighten, or a batch shows failing boards.

## One server

- **Problem.** The site, the database, and the pipeline need a home, within a small budget.
- **Options.** Managed services (RDS, ECS, managed Airflow); one EC2 instance with Docker
  Compose.
- **Choice.** One EC2 t3.small in us-east-2 running Docker Compose, with Caddy for HTTPS,
  Session Manager instead of SSH, and S3 for raw responses and backups.
- **Benefit.** About $20.58 a month at list price before credits (estimate,
  [SIZING.md](../deploy/SIZING.md)), simple to reason about, and rebuildable from the runbook
  and an S3 backup.
- **Cost.** No high availability: if the instance fails, the site is down until it is
  rebuilt, and up to a day of collected data can be lost.
- **Revisit when** the site needs uptime guarantees, or memory measured on the instance shows
  the stack no longer fits.

## Tableau Public through a Google Sheet

- **Problem.** I wanted public dashboards about the market, updated automatically, at no cost.
- **Options.** Charts in Python on the website; Tableau Public connected to the database
  (not possible: it cannot reach a private database); Tableau Public reading a Google Sheet.
- **Choice.** After each successful publication, the dashboard tables are written to a Google
  Sheet in one all-or-nothing request, by a service account that can edit only that Sheet.
  Tableau Public reads the Sheet, and the website will embed the dashboards once the workbook
  is published (the Sheet updates already run; the workbook is in progress).
- **Benefit.** Free, refreshed without manual steps, and only published report data ever
  leaves the server.
- **Cost.** Tableau Public refreshes about once a day, so dashboards can lag the reports, and
  everything in the workbook is public.
- **Revisit when** a dashboard needs fresher data than once a day.

## Smaller settings

Values that change product behavior, decided before they went into the code.

| Setting | Value | Where |
|---|---|---|
| Collection times | 8:00, 12:00, and 21:00 America/Chicago (`JOBMARKET_COLLECTION_HOURS`); backup and summary after 21:00 only | DAG `jobmarket_collection` |
| Retries after a failed board | 3, about 1, 2, and 3 hours later; checked every 15 minutes | [`incidents.py`](../src/jobmarket/incidents.py), DAG `jobmarket_retries` |
| A retry run more than 5 minutes late | moves the next retry a full hour after it | [`incidents.py`](../src/jobmarket/incidents.py) |
| AI batches | every hour at :30, up to 20 listings; skipped while the extraction worker runs | DAG `jobmarket_extraction` |
| Extraction lanes | new listings first; the backlog may use at most the safety stop minus 200 requests a day (`JOBMARKET_AI_NEW_RESERVE`) | [`extraction.py`](../src/jobmarket/extraction.py) |
| Extraction worker | checks for new work every 15 s when idle; waits 1 hour after a rejected request; 256 MB memory limit; 150 s to finish a request on stop | [`extraction_worker.py`](../src/jobmarket/extraction_worker.py), [`docker-compose.yml`](../deploy/docker-compose.yml) |
| AI request retries | 3 tries per job, a 4th when two fail for the same reason (recorded as a repeated error), then give up until reprocess; 10, 30, 90 s between tries, other jobs going ahead; provider errors use no try, and four in a row pause all extraction 15 minutes (2026-11-11; until then rounds of 4 requests, 1 hour apart, at most 5); stop at 90% of the daily limit | [`extraction.py`](../src/jobmarket/extraction.py) |
| AI model | Gemini 3.5 Flash-Lite, minimal thinking, free tier | [`extraction.py`](../src/jobmarket/extraction.py) |
| Answers whose quotes are not found | kept, marked `valid_with_warnings` | [`extraction_check.py`](../src/jobmarket/extraction_check.py) |
| Availability | possibly unavailable after 1 missing complete scan, assumed inactive after 3 | [`availability.py`](../src/jobmarket/availability.py) |
| Board hold | every reference job missing, or at least half and at least 10 | [`availability.py`](../src/jobmarket/availability.py) |
| Raw responses | 4 days on the server, 30 days in S3 | [`raw_store.py`](../src/jobmarket/raw_store.py), [`lifecycle.json`](../deploy/aws/lifecycle.json) |
| Backups | nightly after the 9 PM collection; newest 3 kept locally, 30 days in S3 | DAG, [`backup.py`](../src/jobmarket/backup.py) |
| Airflow history | 30 days | DAG |
| Task time limits | collection 45 min, retry check 30, AI batch 20, publication 20, backup 20, Sheet sync 10, summary and email 10 | DAG |
| Sheet sync | after every successful publication | DAG |
| Search page size | 25 results | [`search.py`](../src/jobmarket/search.py) |
| Search default career level | all levels (early career until 2026-11-07) | [`search.py`](../src/jobmarket/search.py) |
| Newest jobs window | first discovered in the past 3 days; kept jobs, every level, initial imports hidden by default | [`search.py`](../src/jobmarket/search.py) |
| Older jobs in lists | hidden when dated more than 30 days ago (source date, else discovery), unless asked | [`search.py`](../src/jobmarket/search.py) |
| "May be out of date" note | board without a complete collection for 36 hours | [`search.py`](../src/jobmarket/search.py) |
| Experience filter bands | 0-1, 2-4, 5+ years, by the smallest required minimum | [`search.py`](../src/jobmarket/search.py) |
| Sign-in | 8-character minimum, Argon2id; 3 wrong passwords pause sign-in for 15 minutes; sessions end after 2 hours idle or 7 days | [`owner.py`](../src/jobmarket/owner.py) |
| "New since last visit" | a visit starts at sign-in or after 1 hour idle | [`workspace.py`](../src/jobmarket/workspace.py) |
| Profile comparisons | required years count employment only; preference mismatches are gaps labeled "outside your preferences" | [`compare.py`](../src/jobmarket/web/compare.py) |
| Server | t3.small, 2 GiB swap, 1 web worker with 4 threads | [SIZING.md](../deploy/SIZING.md) |
