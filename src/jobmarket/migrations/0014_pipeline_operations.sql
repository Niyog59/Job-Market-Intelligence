-- Scheduled operation: a record of every pipeline step (report publication, backups, raw-file
-- uploads and cleanup, daily summaries), alerts for steps other than collection, and the
-- daily summary email. Everything that decides whether a message is sent lives here, so a
-- restart or a re-run never sends the same message twice.

-- Daily summaries go through the same outbox as alerts. dedup_key makes "one summary per
-- day" and "one alert per incident" a database guarantee rather than a convention.
ALTER TABLE notifications DROP CONSTRAINT notifications_kind_check;
ALTER TABLE notifications ADD CONSTRAINT notifications_kind_check
    CHECK (kind IN ('alert', 'recovery', 'summary'));
ALTER TABLE notifications ADD COLUMN dedup_key text;
CREATE UNIQUE INDEX notifications_dedup_key ON notifications (dedup_key);

COMMENT ON COLUMN notifications.dedup_key IS
    'Optional unique key: "summary:<Chicago date>", "ops-alert:<incident>", '
    '"ops-recovery:<incident>". A second insert with the same key is refused.';

-- One row per run of a pipeline step, written by the step itself.
CREATE TABLE pipeline_step_runs (
    step_run_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    step         text NOT NULL CHECK (step IN (
                     'report_publish', 'backup', 'backup_prune', 'raw_sync', 'raw_cleanup',
                     'daily_summary')),
    started_at   timestamptz NOT NULL,
    finished_at  timestamptz NOT NULL,
    status       text NOT NULL CHECK (status IN ('succeeded', 'failed', 'skipped')),
    detail       jsonb NOT NULL DEFAULT '{}'::jsonb,
    error        text,
    CHECK (finished_at >= started_at),
    CHECK ((status = 'failed') = (error IS NOT NULL))
);

CREATE INDEX pipeline_step_runs_step_time ON pipeline_step_runs (step, finished_at DESC);

-- A failing step other than collection (collection has its own per-board incidents). At
-- most one open incident per step: the first failure queues one alert, later failures only
-- update the incident, and the next success resolves it with one recovery notice.
CREATE TABLE ops_incidents (
    incident_id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    step                     text NOT NULL CHECK (step IN ('report_publish', 'backup')),
    opened_at                timestamptz NOT NULL,
    last_failure_at          timestamptz NOT NULL,
    failures                 integer NOT NULL DEFAULT 1 CHECK (failures >= 1),
    last_error               text NOT NULL,
    alert_notification_id    bigint REFERENCES notifications (notification_id),
    resolved_at              timestamptz,
    recovery_notification_id bigint REFERENCES notifications (notification_id),
    CHECK (last_failure_at >= opened_at),
    CHECK (resolved_at IS NULL OR resolved_at >= opened_at),
    CHECK (recovery_notification_id IS NULL OR resolved_at IS NOT NULL)
);

CREATE UNIQUE INDEX ops_incidents_one_open ON ops_incidents (step) WHERE resolved_at IS NULL;
