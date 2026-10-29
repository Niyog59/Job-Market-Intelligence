-- One row per meaningful content version of a posting. Version 1 is first sight; later
-- versions are edits (updates), never new jobs. retained_when_recorded is the screening
-- decision stored with the version, i.e. what we knew when the content was first seen.
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
    description_omitted is not null as description_omitted
from {{ source('ingestion', 'posting_versions') }}
