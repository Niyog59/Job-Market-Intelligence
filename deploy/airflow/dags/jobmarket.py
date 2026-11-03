"""Airflow schedules for Job Market Intelligence.

Every task runs an existing `jobmarket` command in /app, so the same work can always be done
by hand (`docker compose run --rm jobs jobmarket ...`). Airflow only decides when it runs.

Scheduling rules:
- Collection at 12:00 and 21:00 America/Chicago. The cron is read in that time zone, so the
  local times stay the same across daylight saving changes.
- catchup=False: after downtime the scheduler creates one run, for the latest missed time
  (Airflow's CronTriggerTimetable takes the later of "the most recent past slot" and "the
  slot after the last run"), never one run per missed slot. Collection always scans the
  boards as they are now, so that one run is a fresh scan.
- One task at a time across all DAGs (AIRFLOW__CORE__PARALLELISM=1 in docker-compose.yml),
  and max_active_runs=1 per DAG. The commands also take PostgreSQL advisory locks, so a
  manual run started at the same time stops with exit code 3, which marks the task skipped.
- Company retries are not waits inside a task: each failed board's next retry time is
  stored in the database, and `jobmarket retry-due` runs every 15 minutes and does only the
  retries that are due, so no task holds the slot while waiting.
- AI extraction runs hourly in batches of at most 20 listings, stops at 90% of the free daily
  limit, and pauses itself on a quota error; it is a separate DAG, so it never delays
  collection, reports, or backups for longer than one bounded batch.
- Steps after collection use trigger_rule all_done: a failed collection, backup, or raw
  upload never stops report publication, the summary, or email.
- After a successful report publication, the published dashboard tables are copied to the
  Google Sheet behind the Tableau Public dashboards (decision 2026-11-02: every run).
- The evening run also backs up, prunes local backups, sends the daily summary, and cleans
  Airflow's own history. A run is "evening" when it was scheduled at 17:00 Chicago time or
  later (the 21:00 slot, even if it starts after midnight, or a manual run started in the
  evening).

Decisions (Niyog, 2026-11-01): retry check every 15 minutes; AI hourly, up to 20 per run;
keep 3 local backups and 30 days of Airflow history; the time limits below.
"""

import os
from datetime import timedelta

import pendulum
from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG
from airflow.timetables.trigger import CronTriggerTimetable

TIMEZONE = "America/Chicago"
START = pendulum.datetime(2026, 11, 1, tz=TIMEZONE)
EVENING_FROM_HOUR = 17
SKIP = 99  # exit code that marks a task skipped (BashOperator's default)
BUSY = 3  # jobmarket: another collection, extraction, or publish holds the lock
NOT_CONFIGURED = 2  # jobmarket: a required setting is missing

# Time limits per task (Niyog, 2026-11-01). Raw uploads and Airflow cleanup use the backup
# limit.
COLLECTION_LIMIT = timedelta(minutes=45)
RETRY_CHECK_LIMIT = timedelta(minutes=30)
AI_BATCH_LIMIT = timedelta(minutes=20)
REPORT_LIMIT = timedelta(minutes=20)
BACKUP_LIMIT = timedelta(minutes=20)
SUMMARY_LIMIT = timedelta(minutes=10)
SHEETS_LIMIT = timedelta(minutes=10)  # Niyog, 2026-11-02

KEEP_LOCAL_BACKUPS = 3
AIRFLOW_HISTORY_DAYS = 30
AI_BATCH_SIZE = 20


def is_evening(dag_run) -> bool:
    """Whether a run belongs to the evening slot, by its scheduled time in Chicago."""
    # A scheduled run's logical date is its cron slot (run_after is the same time). A run
    # started late keeps its slot: `airflow dags test DAG DATE` sets run_after to the moment
    # it starts, not to DATE. A manual run without a logical date counts from its start.
    when = dag_run.logical_date or dag_run.run_after
    return pendulum.instance(when).in_tz(TIMEZONE).hour >= EVENING_FROM_HOUR


def evening_only(command: str) -> str:
    """A bash command that runs only in evening runs and is skipped otherwise."""
    return (
        "{% if is_evening(dag_run) %}" + command + "{% else %}"
        f"echo 'Runs after the evening collection only.'; exit {SKIP}" + "{% endif %}"
    )


DEFAULTS = {
    "owner": "jobmarket",
    "retries": 0,  # failures are retried by the next scheduled run, never in a tight loop
    "cwd": os.environ.get("JOBMARKET_WORKDIR", "/app"),
}


def task(task_id: str, command: str, limit: timedelta, **kwargs) -> BashOperator:
    return BashOperator(task_id=task_id, bash_command=command, execution_timeout=limit, **kwargs)


with DAG(
    dag_id="jobmarket_collection",
    description="Collect at noon and 9 PM Chicago time, then publish reports; evenings also "
    "back up and send the daily summary.",
    schedule=CronTriggerTimetable("0 12,21 * * *", timezone=TIMEZONE),
    start_date=START,
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULTS,
    user_defined_macros={"is_evening": is_evening},
    tags=["jobmarket"],
) as collection:
    collect = task("collect", "jobmarket collect --scheduled", COLLECTION_LIMIT,
                   skip_on_exit_code=[SKIP, BUSY], priority_weight=10)  # fmt: skip
    after = {"trigger_rule": "all_done", "priority_weight": 10}
    backup = task("backup", evening_only("jobmarket backup create --upload"), BACKUP_LIMIT,
                  **after)  # fmt: skip
    prune = task("backup_prune",
                 evening_only(f"jobmarket backup prune --keep {KEEP_LOCAL_BACKUPS}"),
                 BACKUP_LIMIT, **after)  # fmt: skip
    raw_sync = task("raw_sync", "jobmarket sync-raw", BACKUP_LIMIT, **after)
    raw_cleanup = task("raw_cleanup", "jobmarket cleanup-raw --require-s3", BACKUP_LIMIT,
                       **after)  # fmt: skip
    publish = task("publish_reports", "jobmarket analytics publish", REPORT_LIMIT,
                   skip_on_exit_code=[SKIP, BUSY], **after)  # fmt: skip
    # Only after a successful publication (the default trigger rule), so the dashboard Sheet
    # never receives reports that failed their checks; skipped until the Sheet is configured.
    sheets_sync = task("sheets_sync", "jobmarket tableau sync", SHEETS_LIMIT,
                       skip_on_exit_code=[SKIP, NOT_CONFIGURED], priority_weight=10)  # fmt: skip
    summary = task("daily_summary", evening_only("jobmarket summary send"), SUMMARY_LIMIT,
                   **after)  # fmt: skip
    notify = task("send_email", "jobmarket notifications --send", SUMMARY_LIMIT, **after)
    # Airflow 3 blocks the metadata database inside task processes (it replaces the
    # connection setting with a dead value). `airflow db clean` is the one task that needs
    # it, so the scheduler container provides the address under JOBMARKET_AIRFLOW_DB_URL and
    # only this command uses it.
    cutoff = (
        "python3 -c 'import datetime as d; print((d.datetime.now(d.timezone.utc) - "
        f'd.timedelta(days={AIRFLOW_HISTORY_DAYS})).isoformat(timespec="seconds"))\''
    )
    cleanup_airflow = task(
        "airflow_history_cleanup",
        evening_only(
            ': "${JOBMARKET_AIRFLOW_DB_URL:?set in docker-compose.yml}" && '
            'AIRFLOW__DATABASE__SQL_ALCHEMY_CONN="$JOBMARKET_AIRFLOW_DB_URL" '
            f'airflow db clean --yes --skip-archive --clean-before-timestamp "$({cutoff})" && '
            f'find "$AIRFLOW_HOME/logs" -type f -mtime +{AIRFLOW_HISTORY_DAYS} -delete && '
            'find "$AIRFLOW_HOME/logs" -mindepth 1 -type d -empty -delete'
        ),
        BACKUP_LIMIT,
        **after,
    )
    (collect >> backup >> prune >> raw_sync >> raw_cleanup >> publish >> sheets_sync >> summary
     >> notify >> cleanup_airflow)  # fmt: skip


with DAG(
    dag_id="jobmarket_retries",
    description="Every 15 minutes: retry the boards whose retry is due, then send alerts.",
    schedule=CronTriggerTimetable("*/15 * * * *", timezone=TIMEZONE),
    start_date=START,
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULTS,
    tags=["jobmarket"],
) as retries:
    retry_due = task("retry_due", "jobmarket retry-due", RETRY_CHECK_LIMIT,
                     skip_on_exit_code=[SKIP, BUSY], priority_weight=5)  # fmt: skip
    alerts = task("send_email", "jobmarket notifications --send", SUMMARY_LIMIT,
                  trigger_rule="all_done", priority_weight=5)  # fmt: skip
    retry_due >> alerts


with DAG(
    dag_id="jobmarket_extraction",
    description="Hourly: one bounded batch of AI requirement extraction (free tier).",
    # Minute 30, so a batch never starts at the same moment as a collection.
    schedule=CronTriggerTimetable("30 * * * *", timezone=TIMEZONE),
    start_date=START,
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULTS,
    tags=["jobmarket"],
) as ai:
    task("extract", f"jobmarket extract run --limit {AI_BATCH_SIZE}", AI_BATCH_LIMIT,
         skip_on_exit_code=[SKIP, BUSY, NOT_CONFIGURED], priority_weight=1)  # fmt: skip
