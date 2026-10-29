-- One row per meaningful content version of a posting. Version 1 is first sight; later
-- versions are edits (updates), never new jobs. retained_when_recorded is the screening
-- decision stored with the version, i.e. what we knew when the content was first seen.
-- salary_range is the structured range a source supplies (Lever's salaryRange); it is never
-- parsed from description text. search_vector uses the indexed ingestion function, so text
-- search over this view can use posting_versions_search_idx.
select
    version_id,
    posting_id,
    version_no,
    detection,
    title,
    location_text,
    first_seen_at,
    recorded_at,
    (screening ->> 'retained')::boolean as retained_when_recorded,
    description_length,
    description_omitted is not null as description_omitted,
    absolute_url as posting_url,
    apply_url,
    case when jsonb_typeof(source_fields -> 'salaryRange') = 'object'
         then source_fields -> 'salaryRange' end as salary_range,
    {{ var('source_schema') }}.posting_search_vector(title, description_text) as search_vector
from {{ source('ingestion', 'posting_versions') }}
