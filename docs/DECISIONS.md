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
  takes time (51 enabled boards so far, with the rejected candidates and their reasons kept
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
| Collection times | 12:00 and 21:00 America/Chicago | DAG `jobmarket_collection` |
| Retries after a failed board | 3, about 1, 2, and 3 hours later; checked every 15 minutes | [`incidents.py`](../src/jobmarket/incidents.py), DAG `jobmarket_retries` |
| A retry run more than 5 minutes late | moves the next retry a full hour after it | [`incidents.py`](../src/jobmarket/incidents.py) |
| AI batches | every hour at :30, up to 20 listings | DAG `jobmarket_extraction` |
| AI request retries | 3 per request (10, 30, 90 s), at most 5 runs per job; stop at 90% of the daily limit | [`extraction.py`](../src/jobmarket/extraction.py) |
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
| "May be out of date" note | board without a complete collection for 36 hours | [`search.py`](../src/jobmarket/search.py) |
| Experience filter bands | 0-1, 2-4, 5+ years, by the smallest required minimum | [`search.py`](../src/jobmarket/search.py) |
| Sign-in | 8-character minimum, Argon2id; 3 wrong passwords pause sign-in for 15 minutes; sessions end after 2 hours idle or 7 days | [`owner.py`](../src/jobmarket/owner.py) |
| "New since last visit" | a visit starts at sign-in or after 1 hour idle | [`workspace.py`](../src/jobmarket/workspace.py) |
| Profile comparisons | required years count employment only; preference mismatches are gaps labeled "outside your preferences" | [`compare.py`](../src/jobmarket/web/compare.py) |
| Server | t3.small, 2 GiB swap, 1 web worker with 4 threads | [SIZING.md](../deploy/SIZING.md) |
