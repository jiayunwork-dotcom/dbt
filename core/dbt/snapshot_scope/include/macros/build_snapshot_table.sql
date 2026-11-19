{#
    First-run path for a snapshot with a scoped hard-delete list.

    When the history table does not exist yet the global materialization builds
    it through the ``build_snapshot_table`` dispatcher rather than the staging
    table, so scope/source validation has to run on this path too. The
    dispatcher is overridden (adapter-agnostic plain macro name, this internal
    package takes precedence); snapshots without a scope re-enter the global
    dispatcher pinned to the 'dbt' package. On later runs the staging-table
    override takes over; each path uses distinct validation statement names so
    the two paths can never collide within a single execution.
#}
{% macro build_snapshot_table(strategy, sql) %}

    {%- set scope_marker, scope_key_column = get_hard_delete_scope_config() -%}

    {%- if scope_marker is none -%}
        {{ return(adapter.dispatch('build_snapshot_table', 'dbt')(strategy, sql)) }}
    {%- endif -%}

    {%- set scope_relation = hard_delete_scope_relation(scope_marker) -%}

    {%- if adapter.get_hard_deletes_behavior(config) == 'ignore' -%}
        {{ exceptions.raise_compiler_error(
            "'hard_deletes_scope' is set but 'hard_deletes' is 'ignore' (or unset). "
            ~ "Set hard_deletes to 'invalidate' or 'new_record' to use a scoped hard-delete list, "
            ~ "or remove hard_deletes_scope."
        ) }}
    {%- endif -%}

    {{ validate_hard_delete_scope(scope_relation, scope_key_column, strategy, sql, false) }}

    {{ return(adapter.dispatch('build_snapshot_table', 'dbt')(strategy, sql)) }}

{% endmacro %}
