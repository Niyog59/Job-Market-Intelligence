# Problems and fixes

What went wrong, where it happened, what I found, and what changed. Each entry says whether
it happened in development (my Mac), in CI (GitHub Actions), or in production (the server
behind https://jobobservatory.dev). Back to the [README](../README.md#problems-i-ran-into).

## New jobs were hard to find on the website

*Production, found 2026-11-06. Fixed in code; see the README for what is deployed.*

- **What happened:** the default search on https://jobobservatory.dev showed 13 jobs, while
  the site had 5,297 active job opportunities and its last collection had found new ones
  that day.
- **What I found:** nothing was waiting on the AI or on dbt. The serving views are live
  views, so collected listings were stored and searchable at once. The defaults hid them:
  kept jobs only (147 of 5,297) and early career only (13 of 147). A kept job that the AI
  read and marked "not early career" dropped out of the default list. On top of that,
  collection ran at noon and 9 PM only.
- **What changed:** a public Newest jobs page that lists every job found in the past 3
  days, newest first, before the AI reads it, with only the rule-based screening as filters
  and an Apply link on every row ([decision](DECISIONS.md#show-new-listings-before-extraction)).
  Collection also runs at 8 AM now, which cuts the overnight wait from 15 hours to 11
  ([decision](DECISIONS.md#three-collections-a-day)).
- **How I checked it:** [`test_newest.py`](../tests/test_newest.py) collects a board with a
  scripted API: an unextracted listing appears with its application link, sorts by
  discovery, is not hidden by unknown requirements, keeps its place after a failed scan,
  and gains its extracted details later as the same row.

## Google "high demand" errors used up a day of AI requests

*Production, 2026-11-04 and 2026-11-05. Not fixed yet.*

- **What happened:** by 9 PM on 2026-11-04 the Reports page showed 124 AI requests, 34 of
  them failed. The next day, 352 of the day's 450 requests failed, and extraction stopped at the
  daily safety limit by early afternoon (Chicago time) with 98 listings done.
- **What I found:** every one of the 449 server errors over those two days was the same
  HTTP 503 from Google: "This model is currently experiencing high demand." Of the 34
  failures counted by 9 PM on 2026-11-04, 32 were that error, 1 was a read timeout, and 1 was a per-minute
  rate limit.
- **Failed requests are not failed listings.** The 34 failed requests touched 14 listings.
  Ten got an answer on a retry within the same round, and four failed a whole round of four
  tries, waited an hour, and then got an answer. None was given up and none is still waiting.
- **Why it still matters:** every try counts toward the free daily limit. When Google keeps
  failing, each listing uses four tries and then the next listing does the same, so an outage
  spends the day's requests on errors. On 2026-11-05 all of those requests came from the
  hourly batch; the extraction worker started after the limit was already reached.
- **What is next:** a pause after a listing fails a whole round on provider errors, so the
  worker waits for Google to recover instead of moving straight to the next listing. Not
  built yet. Retry handling today: [`extraction.py`](../src/jobmarket/extraction.py)
  (`Session._process`).

## AI extraction was falling behind

*Production. The fix runs on the server since 2026-11-05; its speed there is not measured
yet.*

- **What happened:** new listings waited hours for AI extraction. On 2026-11-05 the oldest
  waiting new listing had been in the queue for 38.8 hours.
- **Why:** extraction ran as an hourly batch of at most 20 listings, and that batch shared
  Airflow's single task slot with collection, reports, and backups. When a collection found
  more than 20 new listings, the rest waited for later hours. Each batch took a minute or
  two, so the free per-minute allowance sat unused for the rest of the hour.
- **What changed:** extraction moved into a dedicated worker
  ([`extraction_worker.py`](../src/jobmarket/extraction_worker.py)) that checks for waiting
  jobs continuously, every 15 seconds when idle. It handles new listings first and works
  through the older backlog with spare capacity, at most 250 requests a day, so 200 always
  stay free for new listings. The queue changes it relies on are in
  [`extraction.py`](../src/jobmarket/extraction.py): each job is claimed by one process at a
  time, and every process shares one rate limit. It is one worker, not several in parallel.
  The hourly batch stays as a fallback and skips itself while the worker runs.
- **What did not change:** the daily API limit. The free tier allows 500 requests a day and
  extraction stops at 450. The worker shortens each new listing's wait; it does not raise how
  many listings a day can be extracted. The backlog of about 4,800 older listings is not
  cleared and still clears only at the free tier's pace.
- **How I checked it:** [`test_extraction_worker.py`](../tests/test_extraction_worker.py)
  covers idling, picking up new work, quota pauses and their reset, and clean shutdown.
  [`test_extraction_lanes.py`](../tests/test_extraction_lanes.py) covers priority, the
  backlog share, crash recovery, and that no two processes take the same job. CI also starts
  and stops the worker container. On its first day in production the worker had nothing to
  send, because of the Google errors above. Speed figures will be added once measured.
- More: [the worker in EXTRACTION.md](EXTRACTION.md#extraction-worker),
  [the decision](DECISIONS.md#a-separate-extraction-worker).

## Slow queries right after a publication

*Development, found during the Spark comparison on 2026-11-03. Fixed.*

- **What happened:** an export started a few seconds after a publication ran one query for
  more than ten minutes.
- **Why:** a publication creates new tables, and new tables have no planner statistics until
  autovacuum analyzes them, so PostgreSQL planned with default estimates. The website's first
  queries after a publication had the same exposure.
- **What changed:** the publish step runs `ANALYZE` on every table of the new build before the
  swap ([`analytics.py`](../src/jobmarket/analytics.py), `_analyze`).
- **How I checked it:** the same export finished in 22 seconds once the tables had statistics,
  and [`test_published_tables_have_planner_statistics`](../tests/test_publish.py) fails if a
  published table was never analyzed.

## Evening runs that started after midnight skipped their evening duties

*CI failure, not a production incident. Fixed.*

- **What happened:** in CI, the deploy job's check of recorded steps failed because the
  21:00 run had skipped its backup, prune, and summary steps.
- **Why:** the DAG decided "evening" from the run's start time, and `airflow dags test` sets
  that to the moment the test starts. That CI run started after midnight in Chicago. A real
  run delayed past midnight would have done the same.
- **What changed:** the evening duties follow the run's logical date (its scheduled slot), and
  only manual runs without one use their start time
  ([`jobmarket.py`](../deploy/airflow/dags/jobmarket.py), `is_evening`).
- **How I checked it:** tests cover a 21:00 run started after midnight
  ([`test_schedule.py`](../deploy/airflow/tests/test_schedule.py),
  [`test_pipeline_runs.py`](../deploy/airflow/tests/test_pipeline_runs.py)), and the deploy
  job passes whatever time it runs.

## No HTTPS certificate on the first deployment

*Production setup on 2026-11-04, before the site was public. Fixed.*

- **What happened:** during the first setup on the server, the site did not load over HTTPS.
- **Why:** the Caddy log showed Let's Encrypt refusing a certificate for
  `change_me.duckdns.org`: the server's settings file still had the placeholder domain from
  the example file.
- **What changed:** I set the real domain and recreated the Caddy container (a plain restart
  keeps the old setting). The example file now asks for the domain, and the
  [runbook](../deploy/RUNBOOK.md) checks it and lists the symptom under troubleshooting.
- **How I checked it:** Caddy obtained the certificate, and HTTPS, the HTTP redirect, and the
  closed database port were checked from outside the server.
