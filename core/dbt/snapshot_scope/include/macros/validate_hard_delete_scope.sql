{#
    Runtime validation for a snapshot with a scoped hard-delete list.

    Every check raises a compiler error *before* the snapshot staging table is
    built or anything is merged into the existing history table. DuckDB (and
    the other transactional adapters) run the materialization inside a
    transaction, so even statements executed before the failure are rolled
    back; the history therefore stays byte-for-byte unchanged.

    All checks run as a small, fixed number of validation queries:

      1. one metadata call confirming the scope relation exposes the
         configured warehouse column;
      2. one aggregate query over the scope list counting null/blank warehouse
         ids;
      3. one aggregate query over the observed source query counting empty
         unique-key components and duplicate (warehouse, product) keys.

    An empty scope list (zero rows) is valid: it simply means no warehouse may
    be treated as completely reported, so only observed rows update history.

    Each materialization path (first-run build, later-run staging) invokes
    this macro exactly once, and every statement uses a distinct result name,
    so no validation query is ever executed twice within a run.
#}
{% macro validate_hard_delete_scope(scope_relation, scope_key_column, strategy, source_sql, target_relation_exists) -%}

    {%- set unique_key = strategy.unique_key -%}
    {%- if unique_key is string -%}
        {%- set key_columns = [unique_key] -%}
    {%- else -%}
        {%- set key_columns = unique_key -%}
    {%- endif -%}

    {%- if scope_key_column not in key_columns -%}
        {{ exceptions.raise_compiler_error(
            "'hard_deletes_scope_key' ('" ~ scope_key_column
            ~ "') must be one of the snapshot's unique_key columns: "
            ~ (key_columns | join(', '))
        ) }}
    {%- endif -%}

    {#- 1 & 2: the scope list must exist and expose the warehouse column. -#}
    {%- set scope_columns = adapter.get_columns_in_relation(scope_relation) | map(attribute='name') | list -%}
    {%- if scope_key_column not in scope_columns -%}
        {{ exceptions.raise_compiler_error(
            "The hard-delete scope relation " ~ scope_relation.render()
            ~ " is missing the required warehouse column '" ~ scope_key_column
            ~ "'. Columns found: " ~ (scope_columns | join(', '))
        ) }}
    {%- endif -%}

    {#- 3: no null or blank warehouse id on the complete-reporting list. -#}
    {%- call statement('hard_delete_scope_check_nulls_' ~ target_relation_exists, fetch_result=true) -%}
        select count(*) as bad_scope_rows from {{ scope_relation }}
        where {{ adapter.quote(scope_key_column) }} is null
           or cast({{ adapter.quote(scope_key_column) }} as varchar) = ''
    {%- endcall -%}
    {%- set null_scope_result = load_result('hard_delete_scope_check_nulls_' ~ target_relation_exists) -%}
    {%- set null_scope_count = null_scope_result.table.rows[0][0] -%}
    {%- if null_scope_count > 0 -%}
        {{ exceptions.raise_compiler_error(
            "The hard-delete scope relation " ~ scope_relation.render()
            ~ " contains " ~ null_scope_count
            ~ " row(s) with a null or empty '" ~ scope_key_column
            ~ "'. Fix the complete-reporting warehouse list before snapshotting."
        ) }}
    {%- endif -%}

    {#
       4 & 5: observed batch data must have non-empty keys and at most one row
       per (warehouse, product). Checked against the source query itself, so
       the guarantee holds regardless of how the staging table is materialized
       and applies equally to the first snapshot run and later runs.
    #}
    {%- set key_is_null_conditions = [] -%}
    {%- for key_column in key_columns -%}
        {%- do key_is_null_conditions.append(
            "subq." ~ adapter.quote(key_column) ~ " is null or cast(subq."
            ~ adapter.quote(key_column) ~ " as varchar) = ''"
        ) -%}
    {%- endfor -%}

    {%- set key_group_columns = [] -%}
    {%- for key_column in key_columns -%}
        {%- do key_group_columns.append(adapter.quote(key_column)) -%}
    {%- endfor -%}

    {%- call statement('hard_delete_scope_check_source_' ~ target_relation_exists, fetch_result=true) -%}
        with source_key_check as (
            select
                case when {{ key_is_null_conditions | join(' or ') }} then 1 else 0 end as has_empty_key,
                count(*) over (partition by {{ key_group_columns | join(', ') }}) as key_row_count
            from (
                {{ source_sql }}
            ) subq
        )
        select
            coalesce(sum(has_empty_key), 0) as empty_key_rows,
            coalesce(sum(case when key_row_count > 1 then 1 else 0 end), 0) as duplicate_key_rows
        from source_key_check
    {%- endcall -%}
    {%- set source_check_result = load_result('hard_delete_scope_check_source_' ~ target_relation_exists) -%}
    {%- set empty_key_rows = source_check_result.table.rows[0][0] -%}
    {%- set duplicate_key_rows = source_check_result.table.rows[0][1] -%}
    {%- if empty_key_rows > 0 -%}
        {{ exceptions.raise_compiler_error(
            "The snapshot source contains " ~ empty_key_rows
            ~ " row(s) with a null or empty unique key component. Refusing to "
            ~ "snapshot until the batch data is fixed; existing history is unchanged."
        ) }}
    {%- endif -%}
    {%- if duplicate_key_rows > 0 -%}
        {{ exceptions.raise_compiler_error(
            "The snapshot source contains " ~ duplicate_key_rows
            ~ " row(s) sharing a unique key (" ~ (key_columns | join(', '))
            ~ "). A warehouse must report each product at most once per batch. "
            ~ "Refusing to snapshot; existing history is unchanged."
        ) }}
    {%- endif -%}

{%- endmacro %}
