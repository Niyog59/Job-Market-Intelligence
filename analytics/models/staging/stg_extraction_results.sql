-- One row per validated AI extraction of one posting version under one configuration.
-- created_at is when the answer became known; renormalized_at marks a later Python clean-up
-- of the same answer (no new model call).
select
    result_id,
    posting_id,
    version_id,
    config_key,
    model,
    prompt_version,
    schema_version,
    normalization_version,
    validation_status,
    created_at,
    renormalized_at,
    normalized,
    derived
from {{ source('ingestion', 'extraction_results') }}
