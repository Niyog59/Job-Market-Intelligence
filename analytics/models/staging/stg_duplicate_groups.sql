-- One row per duplicate group. Only strong (automatic) and reviewed (confirmed by a person)
-- groups exist here; possible duplicates are flags, not groups.
select
    group_id,
    rule as group_rule,
    confidence as group_confidence,
    status as group_status,
    created_at,
    dissolved_at
from {{ source('ingestion', 'duplicate_groups') }}
