{#
    Scoped hard deletes for snapshots.

    A snapshot may declare the batch-level list of warehouses that reported
    *completely* in the current batch:

        {% snapshot inventory_snapshot %}
        {{ config(
            unique_key=['warehouse_id', 'sku'],
            strategy='check',
            check_cols=['quantity'],
            hard_deletes='new_record',
            hard_deletes_scope=hard_delete_scope_ref('complete_warehouses'),
            hard_deletes_scope_key='warehouse_id'
        ) }}
        ...
        {% endsnapshot %}

    Nothing in this file changes behavior for snapshots that do not set
    ``hard_deletes_scope``. Such snapshots keep using the unmodified global
    hard-delete semantics.
#}


{#
    Parse-time entry point.

    Usage in a snapshot:

        {% set scope_relation = ref('complete_warehouses') %}
        {{ config(
            ...,
            hard_deletes_scope=hard_delete_scope_ref('complete_warehouses'),
            hard_deletes_scope_key='warehouse_id'
        ) }}

    dbt's static parser only extracts ``ref()`` / ``source()`` calls that are
    literally present in the snapshot source text; a call hidden inside another
    macro never reaches the dependency graph. The explicit ``{% set ... =
    ref() %}`` line therefore registers the scope model as an ordinary DAG edge
    -- it shows up in ``dbt ls``/the manifest, makes ``dbt build`` build the
    scope list first, and drives partial-parsing invalidation when the edge
    changes -- while this macro turns the same node name into a plain string
    marker that serializes safely into the manifest. At run time the marker is
    re-resolved to the real relation by ``hard_delete_scope_relation``.
#}
{% macro hard_delete_scope_ref(scope_node) %}hard_delete_scope:ref:{{ scope_node }}{% endmacro %}

{% macro hard_delete_scope_source(scope_source_name, scope_table_name) %}hard_delete_scope:source:{{ scope_source_name }}.{{ scope_table_name }}{% endmacro %}


{#
    Read the two scope config values, applying defaults and basic shape
    validation that does not require database access (safe at parse time too).
    Returns (scope_marker, scope_key_column).
#}
{% macro get_hard_delete_scope_config() -%}
    {%- set scope_marker = config.get('hard_deletes_scope') -%}
    {%- set scope_key_column = config.get('hard_deletes_scope_key', 'warehouse_id') -%}

    {%- if scope_marker is not none -%}
        {%- if not (hard_delete_scope_is_str(scope_marker) and scope_marker.startswith('hard_delete_scope:')) -%}
            {{ exceptions.raise_compiler_error(
                "Invalid 'hard_deletes_scope' config: " ~ scope_marker
                ~ ". Use hard_delete_scope_ref('model_name') or "
                ~ "hard_delete_scope_source('source_name', 'table_name') to reference the "
                ~ "complete-reporting warehouse list for this batch."
            ) }}
        {%- endif -%}
        {%- if not hard_delete_scope_is_str(scope_key_column) or scope_key_column | trim | length == 0 -%}
            {{ exceptions.raise_compiler_error(
                "'hard_deletes_scope_key' must name the warehouse column on the scope list "
                ~ "and on the snapshot's unique key."
            ) }}
        {%- endif -%}
    {%- endif -%}
    {{ return([scope_marker, scope_key_column | trim]) }}
{%- endmacro %}


{% macro hard_delete_scope_is_str(value) -%}
    {{ return(value is string) }}
{%- endmacro %}


{#
    Resolve the parse-time marker back into a runtime relation. Only called at
    run time, where ref/source return real, fully-qualified relations.
#}
{% macro hard_delete_scope_relation(scope_marker) -%}
    {%- if scope_marker.startswith('hard_delete_scope:ref:') -%}
        {%- set ref_name = scope_marker[('hard_delete_scope:ref:' | length):] -%}
        {{ return(ref(ref_name)) }}
    {%- elif scope_marker.startswith('hard_delete_scope:source:') -%}
        {%- set rest = scope_marker[('hard_delete_scope:source:' | length):] -%}
        {%- set parts = rest.split('.') -%}
        {%- if parts | length != 2 -%}
            {{ exceptions.raise_compiler_error(
                "Invalid source marker in 'hard_deletes_scope': " ~ scope_marker
            ) }}
        {%- endif -%}
        {{ return(source(parts[0], parts[1])) }}
    {%- else -%}
        {{ exceptions.raise_compiler_error(
            "Invalid 'hard_deletes_scope' config marker: " ~ scope_marker
        ) }}
    {%- endif -%}
{%- endmacro %}


{#
    Predicate restricting hard-delete candidates to warehouses present on the
    complete-reporting list. The same subquery is embedded in every hard-delete
    CTE of the staging statement, so staging and the final merge reason about
    exactly the same batch scope: no intermediate table or separately cached
    warehouse list can drift between the two.

    The left-hand column is explicitly qualified to ``snapshotted_data``: the
    hard-delete CTEs join snapshotted rows to the observed query, so an
    unqualified warehouse column would be ambiguous and DuckDB (and other
    adapters) could bind it to either side of the left join.
#}
{% macro hard_delete_scope_predicate(scope_relation, scope_key_column) -%}
    snapshotted_data.{{ adapter.quote(scope_key_column) }} in (
        select {{ adapter.quote(scope_key_column) }}
        from {{ scope_relation }}
        where {{ adapter.quote(scope_key_column) }} is not null
    )
{%- endmacro %}
