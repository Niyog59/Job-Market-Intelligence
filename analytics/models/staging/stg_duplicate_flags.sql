-- One row per possible-duplicate pair flagged for review (never merged).
select
    flag_id,
    posting_id,
    related_posting_id,
    rule as flag_rule,
    confidence as flag_confidence,
    status as flag_status,
    detected_at,
    reviewed_at
from {{ source('ingestion', 'posting_duplicate_flags') }}
