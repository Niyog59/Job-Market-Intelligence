{#- Fails for every combination of the given columns that appears more than once. -#}
{% test unique_combination(model, columns) %}
select {{ columns | join(', ') }}, count(*) as rows_with_this_combination
from {{ model }}
group by {{ columns | join(', ') }}
having count(*) > 1
{% endtest %}
