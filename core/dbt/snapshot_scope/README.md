# Scoped hard deletes for snapshots (`hard_deletes_scope`)

## Why

A normal snapshot with `hard_deletes` treats *every* key missing from the
current query as deleted. That is only correct when the query is a **complete**
extract. When a feed is split per warehouse and one warehouse only delivered
half its batch, the products it did not send are not really deleted — they just
were not reported.

`hard_deletes_scope` adds an optional, batch-level **complete-reporting list**:
the set of warehouses whose extract for this batch is known to be whole. A
missing key is only closed/deleted when the warehouse it belongs to appears on
that list.

## Configuration

```sql
-- models/complete_warehouses.sql
-- One column: the warehouse ids that reported completely in this batch.
-- A warehouse with zero products still appears here.
{{ config(materialized='table') }}
select warehouse_id from complete_warehouses_batch
```

```sql
-- snapshots/inventory_snapshot.sql
{% snapshot inventory_snapshot %}
{#
  The literal ref() registers the scope list as an ordinary graph dependency
  (build ordering, partial parsing, `dbt ls`). Keep this line even though the
  variable is not otherwise referenced.
#}
{%- set scope_relation = ref('complete_warehouses') -%}

{{ config(
    target_schema='snapshots',
    strategy='timestamp',
    unique_key=['warehouse_id', 'sku'],
    updated_at='updated_at',

    -- Existing snapshot metadata config, unchanged
    hard_deletes='new_record',          -- or 'invalidate'

    -- New, optional
    hard_deletes_scope=hard_delete_scope_ref('complete_warehouses'),
    hard_deletes_scope_key='warehouse_id'
) }}

select * from {{ ref('inventory') }}
{% endsnapshot %}
```

A source-backed list works the same way:

```sql
{%- set scope_relation = source('batch', 'complete_warehouses') -%}
{{ config(
    ...,
    hard_deletes_scope=hard_delete_scope_source('batch', 'complete_warehouses'),
    hard_deletes_scope_key='warehouse_id'
) }}
```

### Config keys

| key | required | meaning |
| --- | --- | --- |
| `hard_deletes_scope` | no | Marker built by `hard_delete_scope_ref(...)` / `hard_delete_scope_source(...)` naming the complete-reporting list. Omit it to keep the legacy global behavior. |
| `hard_deletes_scope_key` | no | The warehouse column on the scope list. Must be one of the snapshot's `unique_key` columns. Defaults to `warehouse_id`. |

`hard_deletes` must still be set to `invalidate` or `new_record`; setting a
scope without an enabled hard-delete mode raises a compiler error.

## Behavior

For every batch:

* **Observed rows** are inserted, updated and revived exactly as before. Both
  the `check` and `timestamp` strategies are reused unchanged.
* A **missing key** is only treated as a hard delete when:
  1. its currently valid history row exists, and
  2. its warehouse is present on the complete-reporting list.
* With `hard_deletes='invalidate'`, the old valid row is closed
  (`dbt_valid_to` set). With `hard_deletes='new_record'`, the old row is
  closed **and** a delete-marker version (`dbt_is_deleted='True'`) is inserted,
  using the same warehouse predicate for both.
* A missing key from a warehouse **not** on the list keeps its currently valid
  row open. Nothing is closed or marked.
* An **empty scope list** means "no warehouse reported completely": observed
  rows still update, but no absence can produce a deletion.
* Re-running the same batch is idempotent — no extra closed versions or delete
  markers are created.
* Snapshots that never set `hard_deletes_scope` run the original global
  implementation byte-for-byte.

### Failures leave history untouched

The run fails (and the existing history is not modified) when:

* the scope relation cannot be read;
* the scope relation is missing the configured warehouse column;
* the scope list contains a null or empty warehouse id;
* the observed query contains a null/empty unique-key component;
* the observed query contains two rows for the same unique key.

Validation runs before the staging table is built and before the merge, inside
the snapshot transaction.

## Where the decision is made

The "which missing records may be processed this run" decision is taken in the
snapshot **staging query**, not in the final merge. The complete-reporting list
is embedded as an inline subquery in both hard-delete CTEs (`deletes` and, for
`new_record`, `deletion_records`) of one staging statement. Because the
predicate text is generated once and reused, the staging computation and the
merge always reason about the same batch scope — there is no separately cached
warehouse set that could drift between them.

## Extra queries

Compared with a normal snapshot, each scoped run adds a fixed, small number of
queries before the staging build:

1. one metadata call to read the scope relation's columns;
2. one aggregate over the scope list counting null/blank warehouse ids;
3. one aggregate over the observed query counting empty keys and duplicate
   keys.

The scope list itself is also read once more inline inside the staging
statement's delete CTEs. No extra write or temporary warehouse table is needed.

## Graph, build order and partial parsing

The literal `ref()` / `source()` in the snapshot file makes the scope list a
normal manifest dependency:

* it appears in `dbt ls` and the manifest `depends_on`;
* `dbt build` that selects both builds the list first, then the snapshot;
* changing the referenced list rewires the dependency and the build order even
  when the partial-parsing cache is reused;
* `dbt parse` completes while neither the scope list nor any batch table has
  been created yet.
