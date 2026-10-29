-- Joins to versions, screenings, and extractions never multiply postings.
select (select count(*) from {{ ref('int_current_postings') }}) as current_rows,
       (select count(*) from {{ ref('stg_postings') }}) as postings
where (select count(*) from {{ ref('int_current_postings') }})
   <> (select count(*) from {{ ref('stg_postings') }})
