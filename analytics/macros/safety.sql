{#- Refuse to run if any analytics schema would be the ingestion schema: rebuilds drop and
    recreate their own tables and views, so they must never share a schema with raw data. -#}
{% macro assert_analytics_schemas_are_separate() %}
    {%- set source = var('source_schema') | lower -%}
    {%- for name in ['analytics_staging', 'analytics_intermediate', 'analytics_reporting',
                     'analytics_serving'] -%}
        {%- if (var('schema_prefix') ~ name) | lower == source -%}
            {{ exceptions.raise_compiler_error(
                "analytics schema " ~ var('schema_prefix') ~ name ~
                " is the ingestion schema; refusing to build") }}
        {%- endif -%}
    {%- endfor -%}
    select 1
{% endmacro %}

{#- A timestamp's calendar day in the reporting time zone (America/Chicago by default). -#}
{% macro reporting_day(ts) -%}
    (({{ ts }}) at time zone '{{ var("reporting_timezone") }}')::date
{%- endmacro %}

{#- The instant a reporting day ends (start of the next local day), as timestamptz. -#}
{% macro day_end(day) -%}
    ((({{ day }}) + 1)::timestamp at time zone '{{ var("reporting_timezone") }}')
{%- endmacro %}
