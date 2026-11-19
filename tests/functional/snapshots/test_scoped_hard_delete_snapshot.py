"""Functional tests for scoped hard deletes on snapshots.

A snapshot with ``hard_deletes_scope`` only closes/deletes absent keys whose
warehouse is on the batch's complete-reporting list. Absent keys from a
warehouse that reported partially keep their currently valid history row.

The fixtures use generic SQL (CTEs over ``values``) so the same test runs on
the standard test adapter without external seed files.
"""

import pytest

from dbt.tests.util import run_dbt


# Two warehouses on the first batch; only EAST reports completely on the
# second batch, where it drops one product and WEST stops reporting one.
INVENTORY_MODELS = """
{{ config(materialized='table') }}
select * from {{ ref('inventory_source') }}
"""

# The "source" model is rewritten between runs via project.run_sql so the test
# can change the observed batch without touching project files.
INVENTORY_SOURCE_SQL = """
{{ config(materialized='table') }}
select
    cast('E1' as {{ dbt.type_string() }}) as warehouse_id,
    cast('SKU1' as {{ dbt.type_string() }}) as sku,
    cast(100 as {{ dbt.type_int() }}) as qty,
    {{ dbt.current_timestamp() }} as updated_at
"""

SCOPE_LIST_SQL = """
{{ config(materialized='table') }}
select cast(warehouse_id as {{ dbt.type_string() }}) as warehouse_id
from {{ ref('scope_source') }}
"""

SCOPE_SOURCE_SQL = """
{{ config(materialized='table') }}
select cast('E1' as {{ dbt.type_string() }}) as warehouse_id
union all
select cast('W1' as {{ dbt.type_string() }})
"""

SCOPED_SNAPSHOT_SQL = """
{% snapshot scoped_inventory_snapshot %}
{%- set scope_relation = ref('scope_list') -%}
{{ config(
    target_schema=schema,
    strategy='timestamp',
    unique_key=['warehouse_id', 'sku'],
    updated_at='updated_at',
    hard_deletes='new_record',
    hard_deletes_scope=hard_delete_scope_ref('scope_list'),
    hard_deletes_scope_key='warehouse_id',
) }}
select * from {{ ref('inventory_model') }}
{% endsnapshot %}
"""

LEGACY_SNAPSHOT_SQL = """
{% snapshot legacy_inventory_snapshot %}
{{ config(
    target_schema=schema,
    strategy='timestamp',
    unique_key=['warehouse_id', 'sku'],
    updated_at='updated_at',
    hard_deletes='new_record',
) }}
select * from {{ ref('inventory_model') }}
{% endsnapshot %}
"""


class TestScopedHardDeletes:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "inventory_source.sql": INVENTORY_SOURCE_SQL,
            "inventory_model.sql": INVENTORY_MODELS,
            "scope_source.sql": SCOPE_SOURCE_SQL,
            "scope_list.sql": SCOPE_LIST_SQL,
        }

    @pytest.fixture(scope="class")
    def snapshots(self):
        return {
            "scoped.sql": SCOPED_SNAPSHOT_SQL,
            "legacy.sql": LEGACY_SNAPSHOT_SQL,
        }

    def _rebuild_observed(self, project, rows):
        # rows: list of (warehouse_id, sku, qty)
        project.run_sql(f"drop table if exists {project.test_schema}.inventory_source")
        if not rows:
            project.run_sql(
                f"create table {project.test_schema}.inventory_source as "
                f"select cast(null as {project.adapter.quote('')} varchar) as warehouse_id"
            )
            project.run_sql(
                f"truncate table {project.test_schema}.inventory_source"
            )
            return
        values = ", ".join(
            f"('{w}', '{s}', {q}, {project.adapter.dispatch('current_timestamp')()})"
            for w, s, q in rows
        )
        project.run_sql(
            f"create table {project.test_schema}.inventory_source as "
            f"select * from (values {values}) as t(warehouse_id, sku, qty, updated_at)"
        )

    def test_scope_limits_deletes_to_complete_warehouses(self, project):
        run_dbt(["run"])
        run_dbt(["snapshot"])

        relation = f"{project.database}.{project.test_schema}.scoped_inventory_snapshot"

        # Second batch: EAST complete and missing SKU2; WEST partial and
        # missing SKU9. Only the EAST absence may be treated as a delete.
        project.run_sql(
            f"create or replace table {project.test_schema}.scope_source as "
            f"select 'E1' as warehouse_id"
        )
        project.run_sql(
            f"create or replace table {project.test_schema}.inventory_source as "
            f"select * from (values "
            f"('E1', 'SKU1', 110, {project.adapter.dispatch('current_timestamp')()}), "
            f"('W1', 'SKU8', 3, {project.adapter.dispatch('current_timestamp')()})"
            f") as t(warehouse_id, sku, qty, updated_at)"
        )
        run_dbt(["run"])
        run_dbt(["snapshot"])

        # WEST SKU9 must still have a valid row
        west_valid = project.run_sql(
            f"select count(*) from {relation} "
            f"where warehouse_id='W1' and sku='SKU9' and dbt_valid_to is null "
            f"and dbt_is_deleted='False'",
            fetch="one",
        )
        assert west_valid[0] == 1

        # EAST SKU2 must have been closed and carry a delete marker
        east_marker = project.run_sql(
            f"select count(*) from {relation} "
            f"where warehouse_id='E1' and sku='SKU2' and dbt_is_deleted='True'",
            fetch="one",
        )
        assert east_marker[0] == 1

        # No WEST delete markers at all
        west_markers = project.run_sql(
            f"select count(*) from {relation} where warehouse_id='W1' and dbt_is_deleted='True'",
            fetch="one",
        )
        assert west_markers[0] == 0

        # The legacy snapshot, with no scope, deletes the WEST key too
        legacy = f"{project.database}.{project.test_schema}.legacy_inventory_snapshot"
        legacy_west_marker = project.run_sql(
            f"select count(*) from {legacy} where warehouse_id='W1' and dbt_is_deleted='True'",
            fetch="one",
        )
        assert legacy_west_marker[0] == 1

    def test_empty_scope_list_allows_only_observed_updates(self, project):
        run_dbt(["run"])
        run_dbt(["snapshot"])
        relation = f"{project.database}.{project.test_schema}.scoped_inventory_snapshot"

        markers_before = project.run_sql(
            f"select count(*) from {relation} where dbt_is_deleted='True'", fetch="one"
        )[0]

        # An empty complete-reporting list: no warehouse may have absences deleted
        project.run_sql(
            f"create or replace table {project.test_schema}.scope_source as "
            f"select 'E1' as warehouse_id where 1=0"
        )
        project.run_sql(
            f"create or replace table {project.test_schema}.inventory_source as "
            f"select * from (values "
            f"('W1', 'SKU8', 3, {project.adapter.dispatch('current_timestamp')()})"
            f") as t(warehouse_id, sku, qty, updated_at)"
        )
        run_dbt(["run"])
        run_dbt(["snapshot"])

        markers_after = project.run_sql(
            f"select count(*) from {relation} where dbt_is_deleted='True'", fetch="one"
        )[0]
        assert markers_after == markers_before
