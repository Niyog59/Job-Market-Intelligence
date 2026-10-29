-- One row per employer.
select employer_id, name as employer_name, corporate_group
from {{ source('ingestion', 'employers') }}
