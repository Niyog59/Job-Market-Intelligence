# Start here: a short guide for reviewers

If you have twenty minutes, these three areas show how the project is built. Each lists a
few files, what I would want you to notice, and the tests that check it. Back to the
[README](../README.md).

## 1. Collection and history

Files: [`store.py`](../src/jobmarket/store.py), [`availability.py`](../src/jobmarket/availability.py),
[`lever.py`](../src/jobmarket/lever.py), [`0003` migration](../src/jobmarket/migrations/0003_incremental_history.sql).

What to notice:

- Identity, content versions, and observations are separate tables
  ([data model](ARCHITECTURE.md#identity-versions-and-observations)). A rerun of the same
  scan adds nothing, and an unchanged listing adds an observation but no version.
- An older response processed late never overwrites newer state: current pointers only move
  forward in collection time.
- Completeness is decided per board. Lever reports no total, so a scan is complete only when
  it reaches a short page; anything else is partial, and partial or failed scans never count
  a job as missing.
- Availability changes only after complete scans or explicit closure evidence, and a mass
  disappearance puts the board on hold instead of hiding its jobs.

Tests: [`test_history.py`](../tests/test_history.py) (for example
`test_older_input_processed_later_does_not_regress_current_state` and
`test_a_b_a_is_preserved_as_three_stages`),
[`test_availability.py`](../tests/test_availability.py)
(`test_failed_and_partial_scans_pause_counting`,
`test_all_jobs_missing_opens_a_hold_even_on_a_small_board`),
[`test_collector.py`](../tests/test_collector.py)
(`test_rerun_is_idempotent_and_records_only_genuine_changes`), and
[`history_scenarios.py`](../tests/history_scenarios.py), which runs the scenarios through the
real collector and prints the SQL evidence.

## 2. dbt modeling and publication

Files: [`analytics/models/`](../analytics/models/), in particular
[`int_posting_days.sql`](../analytics/models/intermediate/int_posting_days.sql) and
[`rpt_weekly_jobs.sql`](../analytics/models/reporting/rpt_weekly_jobs.sql);
[`analytics.py`](../src/jobmarket/analytics.py) (`build_and_publish`).

What to notice:

- Every model documents its grain, and the counting rules keep different things apart:
  postings and opportunities, initial imports and newly discovered jobs, extracted and not
  yet extracted ([counting rules](ANALYTICS.md#counting-rules)).
- `int_posting_days` rebuilds what was known at the end of each day from timestamped
  evidence, so a later closure or extraction never changes an earlier day.
- Publication builds into shadow schemas, runs the data tests, collects planner statistics,
  and swaps the schemas in one transaction. A failed build leaves the previous reports and
  job search untouched.

Tests: [`test_publish.py`](../tests/test_publish.py)
(`test_a_failed_build_keeps_the_live_reports_and_job_search`,
`test_published_tables_have_planner_statistics`, `test_a_reader_never_blocks_the_swap`),
[`test_analytics_reporting.py`](../tests/test_analytics_reporting.py), and the dbt data tests
that run in every build (172 models and tests in CI).

## 3. Scheduling, reliability, and recovery

Files: [`deploy/airflow/dags/jobmarket.py`](../deploy/airflow/dags/jobmarket.py),
[`incidents.py`](../src/jobmarket/incidents.py), [`operations.py`](../src/jobmarket/operations.py),
[`backup.py`](../src/jobmarket/backup.py).

What to notice:

- Every Airflow task is an ordinary `jobmarket` command, so each step also runs by hand.
- Retry state is stored in the database, not in Airflow: a failed board gets three retries
  over about three hours, which survive restarts.
- A failed step never blocks later ones: a failed collection still publishes, backs up, and
  sends the summary. Alerts are deduplicated, with one recovery notice per incident.
- Backups are verified by checksum, uploaded to S3, and confirmed before an older local copy
  is deleted. Restore refuses a database that already has data.
- The evening duties follow the scheduled slot, not the start time
  ([what broke](PROBLEMS.md#evening-runs-that-started-after-midnight-skipped-their-evening-duties)).

Tests: [`deploy/airflow/tests/test_schedule.py`](../deploy/airflow/tests/test_schedule.py)
(`test_the_scheduled_slot_decides_not_the_start_time`, daylight saving changes),
[`test_pipeline_runs.py`](../deploy/airflow/tests/test_pipeline_runs.py)
(`test_the_logical_date_decides_when_the_run_starts_after_midnight`),
[`test_collector.py`](../tests/test_collector.py)
(`test_retries_follow_the_schedule_and_alert_once`),
[`test_operations.py`](../tests/test_operations.py), and
[`test_backup.py`](../tests/test_backup.py)
(`test_prune_never_deletes_the_only_verified_copy`). The CI `deploy` job starts the whole
Compose stack, runs the real DAGs, and restores the backup they made.
