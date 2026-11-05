-- The long-running extraction worker reports here what it is doing: one row per worker
-- process, updated at every change of state and at least every idle check. Whether a worker
-- is alive is decided by its advisory lock, not by this row; the row says what it last did
-- and why it is paused.
CREATE TABLE extraction_workers (
    worker_id     text PRIMARY KEY,
    started_at    timestamptz NOT NULL,
    heartbeat_at  timestamptz NOT NULL,
    state         text NOT NULL CHECK (state IN (
                      'starting', 'working', 'idle', 'paused', 'stopped')),
    detail        text,
    run_id        bigint REFERENCES extraction_runs (run_id),
    stopped_at    timestamptz
);
