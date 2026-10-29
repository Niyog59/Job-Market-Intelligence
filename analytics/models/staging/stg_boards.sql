-- One row per job board (employer x source system x board token).
select
    board_id,
    employer_id,
    source_system,
    board_key as board_token,
    is_active as is_enabled,
    verification_status,
    created_at as registered_at
from {{ source('ingestion', 'source_boards') }}
