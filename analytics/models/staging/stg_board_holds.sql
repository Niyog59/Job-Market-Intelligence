-- One row per mass-disappearance hold on a board.
select hold_id, board_id, opened_at, released_at, status as hold_status, trigger as hold_trigger
from {{ source('ingestion', 'board_holds') }}
