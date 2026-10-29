-- One row per posting: whether it arrived as part of its board's initial import (first seen
-- at or before the board's first complete collection, or while the board has none yet) or
-- was discovered later. "Discovered" means first observed by our pipeline, not necessarily
-- newly published by the employer; the source's own dates are kept separately.
select
    p.posting_id,
    p.board_id,
    p.first_discovered_at,
    {{ reporting_day('p.first_discovered_at') }} as discovery_day,
    bb.baseline_collected_at,
    bb.baseline_collected_at is not null as board_baseline_established,
    case
        when bb.baseline_collected_at is null then 'initial_import'
        when p.first_discovered_at <= bb.baseline_collected_at then 'initial_import'
        else 'discovered'
    end as cohort
from {{ ref('stg_postings') }} p
join {{ ref('int_board_baselines') }} bb on bb.board_id = p.board_id
