-- Grain: exactly one row. When this reporting build ran and the newest evidence it saw, so
-- pages can say how fresh the reports are (reporting tables change only when analytics are
-- rebuilt; the serving views behind job search are always current).
select
    now() as built_at,
    (select max(collected_at) from {{ ref('stg_board_scans') }} where scan_status = 'complete')
        as latest_complete_collection,
    (select max(created_at) from {{ ref('stg_extraction_results') }}) as latest_extraction
