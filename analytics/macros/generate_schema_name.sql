{#- Use the configured schema names as they are (analytics_staging, ...), with an optional
    prefix for throwaway test builds, instead of dbt's default "<target>_<custom>". -#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ var('schema_prefix') }}{{ target.schema }}
    {%- else -%}
        {{ var('schema_prefix') }}{{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
