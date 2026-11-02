-- The pipeline step that copies the published dashboard tables to the Google Sheet behind the
-- Tableau Public dashboards (jobmarket tableau sync) records its runs like the other steps.
ALTER TABLE pipeline_step_runs DROP CONSTRAINT pipeline_step_runs_step_check;
ALTER TABLE pipeline_step_runs ADD CONSTRAINT pipeline_step_runs_step_check CHECK (step IN (
    'report_publish', 'backup', 'backup_prune', 'raw_sync', 'raw_cleanup', 'daily_summary',
    'sheets_sync'));
