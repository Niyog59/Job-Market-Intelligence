-- One row per posting version with its stored description text. Kept out of
-- stg_posting_versions so the models that do not need the text stay light; used only by
-- keyword rules (int_genai_mentions). Text is null when a description was too long to store.
select
    version_id,
    posting_id,
    description_text
from {{ source('ingestion', 'posting_versions') }}
