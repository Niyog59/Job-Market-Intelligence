-- Denominators nest (stating <= extracted <= listed), shares are percentages, and
-- not_stated / ambiguous rows have no share of postings stating a policy.
select * from {{ ref('rpt_eligibility') }}
where not (stating_postings <= extracted_postings and extracted_postings <= listed_postings)
   or share_of_listed not between 0 and 100
   or coalesce(share_of_extracted, 0) not between 0 and 100
   or coalesce(share_of_stating, 0) not between 0 and 100
   or (status in ('not_stated', 'ambiguous', 'not_extracted') and share_of_stating is not null)
