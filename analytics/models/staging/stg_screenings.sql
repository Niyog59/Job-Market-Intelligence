-- One row per posting: the current rule-based screening (relevance, Texas eligibility).
select
    posting_id,
    retained,
    relevant,
    families as rule_families,
    texas_eligibility as rule_texas_eligibility,
    seniority as rule_seniority,
    early_career as rule_early_career,
    needs_review,
    classifier_version,
    screened_at
from {{ source('ingestion', 'posting_screenings') }}
