-- One row per posting version: which generative-AI terms (macro genai_terms) its title and
-- description mention. The employer's own name is removed first, so "Shield AI" or
-- "Indicium AI" never counts as a mention.
with texts as (
    select
        d.version_id,
        d.posting_id,
        regexp_replace(
            coalesce(v.title, '') || ' ' || coalesce(d.description_text, ''),
            -- the employer's name without a "(...)" qualifier, with regex characters escaped
            regexp_replace(split_part(e.employer_name, ' (', 1),
                           '([.^$*+?()\[\]{}|\\-])', '\\\1', 'g'),
            ' ',
            'gi'
        ) as text_without_employer
    from {{ ref('stg_posting_descriptions') }} d
    join {{ ref('stg_posting_versions') }} v on v.version_id = d.version_id
    join {{ ref('stg_postings') }} p on p.posting_id = d.posting_id
    join {{ ref('stg_boards') }} b on b.board_id = p.board_id
    join {{ ref('stg_employers') }} e on e.employer_id = b.employer_id
),

matched as (
    select
        version_id,
        posting_id,
        array_remove(array[
            {%- for t in genai_terms() %}
            case when text_without_employer {{ '~' if t.case_sensitive else '~*' }} '{{ t.pattern }}'
                 then '{{ t.term }}' end{{ ',' if not loop.last }}
            {%- endfor %}
        ], null) as genai_terms
    from texts
)

select
    version_id,
    posting_id,
    cardinality(genai_terms) > 0 as genai_mention,
    genai_terms
from matched
