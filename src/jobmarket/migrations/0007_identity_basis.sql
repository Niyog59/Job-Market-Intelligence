-- Identity of a source record without a usable source id.
--
-- Identity stays board + source job id. When a record has no usable id but its job-specific
-- URL carries one (Greenhouse /jobs/<id> or gh_jid=<id>, Lever jobs.lever.co/<site>/<id>),
-- that id is used and the posting is marked, so its identity can be reviewed. A record with
-- neither is kept as a rejected record with its evidence, never silently dropped. A later
-- record that does carry the id confirms the identity.
ALTER TABLE source_postings
    ADD COLUMN identity_basis text NOT NULL DEFAULT 'source_id'
        CHECK (identity_basis IN ('source_id', 'job_url'));

COMMENT ON COLUMN source_postings.identity_basis IS
    'source_id: the source record''s own id. job_url: the id was taken from the job-specific '
    'URL because the record had none; review the identity.';
