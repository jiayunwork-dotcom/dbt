{#
    Snapshot staging query with batch-scoped hard deletes.

    The global snapshot materialization calls the ``snapshot_staging_table``
    dispatcher macro. Overriding that dispatcher (a plain, adapter-agnostic
    macro name) lets this internal package take precedence without touching an
    installed adapter: the package is registered first in dbt's internal
    package list, and internal macro namespaces merge in reverse order, so its
    macros win over same-named global macros.

    Snapshots without ``hard_deletes_scope`` re-enter the global dispatcher,
    pinned to the 'dbt' package, and behave exactly as before. Snapshots with a
    scope use ``scoped_snapshot_staging_table`` below, whose only difference
    from the global default implementation is the hard-delete CTEs:

      * ``deletes`` (hard_deletes in ('invalidate', 'new_record')) only
        contains currently-valid rows whose warehouse appears on the
        complete-reporting scope list;
      * ``deletion_records`` (hard_deletes = 'new_record') applies the same
        predicate, so closing the old version and inserting the delete version
        always happen for the exact same set of keys.

    Insertions and updates are untouched: every observed row is still written
    to history with the original check/timestamp strategy semantics, including
    rows from warehouses that only reported partially.
#}
{% macro snapshot_staging_table(strategy, source_sql, target_relation) -%}
    {%- set scope_marker, scope_key_column = get_hard_delete_scope_config() -%}

    {%- if scope_marker is none -%}
        {{ return(adapter.dispatch('snapshot_staging_table', 'dbt')(strategy, source_sql, target_relation)) }}
    {%- endif -%}

    {{ return(scoped_snapshot_staging_table(strategy, source_sql, target_relation)) }}
{%- endmacro %}


{% macro scoped_snapshot_staging_table(strategy, source_sql, target_relation) -%}
    {%- set scope_marker, scope_key_column = get_hard_delete_scope_config() -%}
    {%- set scope_relation = hard_delete_scope_relation(scope_marker) -%}

    {%- if adapter.get_hard_deletes_behavior(config) == 'ignore' -%}
        {{ exceptions.raise_compiler_error(
            "'hard_deletes_scope' is set but 'hard_deletes' is 'ignore' (or unset). "
            ~ "Set hard_deletes to 'invalidate' or 'new_record' to use a scoped hard-delete list, "
            ~ "or remove hard_deletes_scope."
        ) }}
    {%- endif -%}

    {#- Fails before the staging table is built, leaving history untouched. -#}
    {{ validate_hard_delete_scope(scope_relation, scope_key_column, strategy, source_sql, true) }}

    {%- set columns = config.get('snapshot_table_column_names') or get_snapshot_table_column_names() -%}
    {%- if strategy.hard_deletes == 'new_record' -%}
        {%- set new_scd_id = snapshot_hash_arguments([columns.dbt_scd_id, snapshot_get_time()]) -%}
    {%- endif -%}
    with snapshot_query as (

        {{ source_sql }}

    ),

    snapshotted_data as (

        select *, {{ unique_key_fields(strategy.unique_key) }}
        from {{ target_relation }}
        where
            {% if config.get('dbt_valid_to_current') %}
		{% set source_unique_key = columns.dbt_valid_to | trim %}
		{% set target_unique_key = config.get('dbt_valid_to_current') | trim %}

                ( {{ equals(source_unique_key, target_unique_key) }} or {{ source_unique_key }} is null )
            {% else %}
                {{ columns.dbt_valid_to }} is null
            {% endif %}

    ),

    insertions_source_data as (

        select *, {{ unique_key_fields(strategy.unique_key) }},
            {{ strategy.updated_at }} as {{ columns.dbt_updated_at }},
            {{ strategy.updated_at }} as {{ columns.dbt_valid_from }},
            {{ get_dbt_valid_to_current(strategy, columns) }},
            {{ strategy.scd_id }} as {{ columns.dbt_scd_id }}

        from snapshot_query
    ),

    updates_source_data as (

        select *, {{ unique_key_fields(strategy.unique_key) }},
            {{ strategy.updated_at }} as {{ columns.dbt_updated_at }},
            {{ strategy.updated_at }} as {{ columns.dbt_valid_from }},
            {{ strategy.updated_at }} as {{ columns.dbt_valid_to }}

        from snapshot_query
    ),

    {%- if strategy.hard_deletes == 'invalidate' or strategy.hard_deletes == 'new_record' %}

    deletes_source_data as (

        select *, {{ unique_key_fields(strategy.unique_key) }}
        from snapshot_query
    ),
    {% endif %}

    insertions as (

        select
            'insert' as dbt_change_type,
            source_data.*
          {%- if strategy.hard_deletes == 'new_record' -%}
            ,'False' as {{ columns.dbt_is_deleted }}
          {%- endif %}

        from insertions_source_data as source_data
        left outer join snapshotted_data
            on {{ unique_key_join_on(strategy.unique_key, "snapshotted_data", "source_data") }}
            where {{ unique_key_is_null(strategy.unique_key, "snapshotted_data") }}
            or ({{ unique_key_is_not_null(strategy.unique_key, "snapshotted_data") }} and (
               {{ strategy.row_changed }} {%- if strategy.hard_deletes == 'new_record' -%} or snapshotted_data.{{ columns.dbt_is_deleted }} = 'True' {% endif %}
            )

        )

    ),

    updates as (

        select
            'update' as dbt_change_type,
            source_data.*,
            snapshotted_data.{{ columns.dbt_scd_id }}
          {%- if strategy.hard_deletes == 'new_record' -%}
            , snapshotted_data.{{ columns.dbt_is_deleted }}
          {%- endif %}

        from updates_source_data as source_data
        join snapshotted_data
            on {{ unique_key_join_on(strategy.unique_key, "snapshotted_data", "source_data") }}
        where (
            {{ strategy.row_changed }}  {%- if strategy.hard_deletes == 'new_record' -%} or snapshotted_data.{{ columns.dbt_is_deleted }} = 'True' {% endif %}
        )
    )

    {%- if strategy.hard_deletes == 'invalidate' or strategy.hard_deletes == 'new_record' %}
    ,
    deletes as (

        select
            'delete' as dbt_change_type,
            source_data.*,
            {{ snapshot_get_time() }} as {{ columns.dbt_valid_from }},
            {{ snapshot_get_time() }} as {{ columns.dbt_updated_at }},
            {{ snapshot_get_time() }} as {{ columns.dbt_valid_to }},
            snapshotted_data.{{ columns.dbt_scd_id }}
          {%- if strategy.hard_deletes == 'new_record' -%}
            , snapshotted_data.{{ columns.dbt_is_deleted }}
          {%- endif %}
        from snapshotted_data
        left join deletes_source_data as source_data
            on {{ unique_key_join_on(strategy.unique_key, "snapshotted_data", "source_data") }}
            where {{ unique_key_is_null(strategy.unique_key, "source_data") }}
            and {{ hard_delete_scope_predicate(scope_relation, scope_key_column) }}

            {%- if strategy.hard_deletes == 'new_record' %}
            and not (
                --avoid updating the record's valid_to if the latest entry is marked as deleted
                snapshotted_data.{{ columns.dbt_is_deleted }} = 'True'
                and
                {% if config.get('dbt_valid_to_current') -%}
                    snapshotted_data.{{ columns.dbt_valid_to }} = {{ config.get('dbt_valid_to_current') }}
                {%- else -%}
                    snapshotted_data.{{ columns.dbt_valid_to }} is null
                {%- endif %}
            )
            {%- endif %}
    )
    {%- endif %}

    {%- if strategy.hard_deletes == 'new_record' %}
        {% set snapshotted_cols = get_list_of_column_names(get_columns_in_relation(target_relation)) %}
        {% set source_col_names = get_columns_in_query(source_sql) %}
    ,
    deletion_records as (

        select
            'insert' as dbt_change_type,
            {%- for col_name in source_col_names -%}
            {%- if col_name in snapshotted_cols -%}
            snapshotted_data.{{ adapter.quote(col_name) }},
            {%- else -%}
            source_data.{{ adapter.quote(col_name) }},
            {%- endif -%}
            {% endfor -%}
            {%- if strategy.unique_key | is_list -%}
                {%- for key in strategy.unique_key -%}
            snapshotted_data.{{ key }} as dbt_unique_key_{{ loop.index }},
                {% endfor -%}
            {%- else -%}
            snapshotted_data.dbt_unique_key as dbt_unique_key,
            {% endif -%}
            {{ snapshot_get_time() }} as {{ columns.dbt_valid_from }},
            {{ snapshot_get_time() }} as {{ columns.dbt_updated_at }},
            snapshotted_data.{{ columns.dbt_valid_to }} as {{ columns.dbt_valid_to }},
            {{ new_scd_id }} as {{ columns.dbt_scd_id }},
            'True' as {{ columns.dbt_is_deleted }}
        from snapshotted_data
        left join deletes_source_data as source_data
            on {{ unique_key_join_on(strategy.unique_key, "snapshotted_data", "source_data") }}
        where {{ unique_key_is_null(strategy.unique_key, "source_data") }}
        and {{ hard_delete_scope_predicate(scope_relation, scope_key_column) }}
        and not (
            --avoid inserting a new record if the latest one is marked as deleted
            snapshotted_data.{{ columns.dbt_is_deleted }} = 'True'
            and
            {% if config.get('dbt_valid_to_current') -%}
                snapshotted_data.{{ columns.dbt_valid_to }} = {{ config.get('dbt_valid_to_current') }}
            {%- else -%}
                snapshotted_data.{{ columns.dbt_valid_to }} is null
            {%- endif %}
            )

    )
    {%- endif %}

    select * from insertions
    union all
    select * from updates
    {%- if strategy.hard_deletes == 'invalidate' or strategy.hard_deletes == 'new_record' %}
    union all
    select * from deletes
    {%- endif %}
    {%- if strategy.hard_deletes == 'new_record' %}
    union all
    select * from deletion_records
    {%- endif %}


{%- endmacro %}
