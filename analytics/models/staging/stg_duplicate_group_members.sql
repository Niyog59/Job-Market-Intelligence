-- One row per posting in a duplicate group.
select group_id, posting_id, added_at
from {{ source('ingestion', 'duplicate_group_members') }}
