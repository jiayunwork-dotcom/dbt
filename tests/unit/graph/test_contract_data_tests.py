import copy
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from unittest import mock

import pytest

from dbt.artifacts.resources import ColumnInfo
from dbt.artifacts.resources.v1.components import Contract
from dbt.contracts.graph.manifest import Manifest
from dbt.contracts.state import PreviousState
from dbt.graph.contract_data_tests import (
    compute_contract_changes,
    select_contract_data_tests,
)
from dbt.graph.selector_methods import MethodManager, StateSelectorMethod
from dbt_common.contracts.constraints import (
    ColumnLevelConstraint,
    ConstraintType,
    ModelLevelConstraint,
)
from tests.unit.utils.manifest import (
    make_generic_test,
    make_macro,
    make_manifest,
    make_model,
    make_singular_test,
)


def make_contracted_model(
    pkg: str,
    name: str,
    columns: Dict[str, str],
    refs=None,
    enforced: bool = True,
    checksum: Optional[str] = None,
    model_constraints: Optional[List[ModelLevelConstraint]] = None,
    column_constraints: Optional[Dict[str, List[ColumnLevelConstraint]]] = None,
):
    model = make_model(pkg, name, f"select 1 -- {name}", refs=refs or [])
    column_constraints = column_constraints or {}
    model.columns = {
        column_name: ColumnInfo(
            name=column_name,
            data_type=data_type,
            constraints=column_constraints.get(column_name, []),
        )
        for column_name, data_type in columns.items()
    }
    model.constraints = model_constraints or []
    model.contract = Contract(enforced=enforced, checksum=checksum)
    return model


def make_not_null(pkg, model, column_name, where=None, fail_calc=None):
    test = make_generic_test(pkg, "not_null", model, {}, column_name=column_name, refs=[model])
    test.attached_node = model.unique_id
    if where is not None:
        test.config.where = where
    if fail_calc is not None:
        test.config.fail_calc = fail_calc
    return test


def make_unique(pkg, model, column_name):
    test = make_generic_test(pkg, "unique", model, {}, column_name=column_name, refs=[model])
    test.attached_node = model.unique_id
    return test


def make_relationships(pkg, model, to_model, column_name, field):
    test = make_generic_test(
        pkg,
        "relationships",
        model,
        {"to": f"ref('{to_model.name}')", "field": field},
        column_name=column_name,
        refs=[model, to_model],
    )
    test.attached_node = model.unique_id
    return test


def make_custom_generic(pkg, model, column_name):
    test = make_generic_test(
        pkg, "positive_value", model, {}, column_name=column_name, refs=[model]
    )
    test.attached_node = model.unique_id
    return test


def previous_state_from(manifest: Manifest) -> PreviousState:
    writable = copy.deepcopy(manifest).writable_manifest()
    state = PreviousState(
        state_path=Path("/path/does/not/exist"),
        target_path=Path("/path/does/not/exist"),
        project_root=Path("/path/does/not/exist"),
    )
    state.manifest = Manifest.from_writable_manifest(writable)
    return state


def search(manifest, previous_state, selector):
    methods = MethodManager(manifest, previous_state)
    method = methods.get_method("state", [])
    assert isinstance(method, StateSelectorMethod)
    return set(
        method.search(
            set(manifest.nodes)
            | set(manifest.sources)
            | set(manifest.exposures)
            | set(manifest.metrics)
            | set(manifest.semantic_models)
            | set(manifest.saved_queries)
            | set(manifest.unit_tests),
            selector,
        )
    )


ORDERS_COLUMNS = {
    "order_id": "integer",
    "amount": "integer",
    "status": "varchar",
    "customer_id": "integer",
}
CUSTOMERS_COLUMNS = {"id": "integer", "name": "varchar"}


def build_manifests(
    orders_columns=ORDERS_COLUMNS,
    customers_columns=CUSTOMERS_COLUMNS,
    orders_enforced=True,
    customers_enforced=True,
    orders_checksum=None,
    customers_checksum=None,
    orders_model_constraints=None,
    extra_current_nodes=(),
    extra_current_macros=(),
) -> Tuple[Manifest, Manifest]:
    """A previous manifest with the baseline models, and a current manifest
    with the given contract variations plus the standard set of tests."""
    orders_v1 = make_contracted_model("pkg", "orders", ORDERS_COLUMNS)
    customers_v1 = make_contracted_model("pkg", "customers", CUSTOMERS_COLUMNS)
    previous = make_manifest(nodes=[orders_v1, customers_v1])

    orders = make_contracted_model(
        "pkg",
        "orders",
        orders_columns,
        enforced=orders_enforced,
        checksum=orders_checksum,
        model_constraints=orders_model_constraints,
    )
    customers = make_contracted_model(
        "pkg",
        "customers",
        customers_columns,
        enforced=customers_enforced,
        checksum=customers_checksum,
    )
    order_summary = make_model("pkg", "order_summary", "select * from orders", refs=[orders])
    downstream = make_model(
        "pkg", "downstream", "select * from order_summary", refs=[order_summary]
    )
    independent = make_contracted_model("pkg", "independent_model", {"x": "integer"})

    tests = [
        make_not_null("pkg", orders, "amount"),
        make_not_null("pkg", orders, "status"),
        make_unique("pkg", orders, "order_id"),
        make_relationships("pkg", orders, customers, "customer_id", "id"),
        make_not_null("pkg", customers, "id"),
        make_unique("pkg", customers, "id"),
        make_not_null("pkg", customers, "name"),
        make_not_null("pkg", order_summary, "total"),
        make_not_null("pkg", independent, "x"),
        make_singular_test(
            "pkg", "summary_positive", "select * from order_summary", refs=[order_summary]
        ),
        make_singular_test(
            "pkg", "downstream_check", "select * from downstream", refs=[downstream]
        ),
        make_singular_test("pkg", "independent_check", "select 1", refs=[independent]),
    ]
    current = make_manifest(
        nodes=[
            orders,
            customers,
            order_summary,
            downstream,
            independent,
            *tests,
            *extra_current_nodes,
        ],
        macros=list(extra_current_macros),
    )
    return previous, current


def uid(test_name):
    return f"test.pkg.{test_name}"


class TestComputeContractChanges:
    def test_no_changes(self):
        previous, current = build_manifests()
        changes = compute_contract_changes(previous, current)
        assert changes.changed_columns == {}
        assert changes.changed_models == set()

    def test_column_type_change(self):
        columns = {**ORDERS_COLUMNS, "amount": "decimal(10,2)"}
        previous, current = build_manifests(orders_columns=columns)
        changes = compute_contract_changes(previous, current)
        assert changes.changed_columns == {"model.pkg.orders": {"amount"}}
        assert changes.changed_models == set()

    def test_column_type_normalization(self):
        # case and whitespace differences are not contract changes
        columns = {**ORDERS_COLUMNS, "amount": "INTEGER", "status": "VARCHAR( 256 )"}
        columns["status"] = "varchar( 256 )"
        previous, current = build_manifests(orders_columns=columns)
        changes = compute_contract_changes(previous, current)
        assert changes.changed_columns == {"model.pkg.orders": {"status"}}
        # "INTEGER" vs "integer" alone is not a change
        columns = {**ORDERS_COLUMNS, "amount": "INTEGER"}
        previous, current = build_manifests(orders_columns=columns)
        changes = compute_contract_changes(previous, current)
        assert changes.changed_columns == {}

    def test_column_added_and_removed(self):
        columns = {k: v for k, v in ORDERS_COLUMNS.items() if k != "status"}
        columns["note"] = "varchar"
        previous, current = build_manifests(orders_columns=columns)
        changes = compute_contract_changes(previous, current)
        assert changes.changed_columns == {"model.pkg.orders": {"status", "note"}}

    def test_column_constraint_change(self):
        previous, current = build_manifests()
        orders = current.nodes["model.pkg.orders"]
        orders.columns["amount"].constraints = [
            ColumnLevelConstraint(type=ConstraintType.check, expression="amount > 0")
        ]
        changes = compute_contract_changes(previous, current)
        assert changes.changed_columns == {"model.pkg.orders": {"amount"}}

    def test_contract_toggle_is_model_level(self):
        previous, current = build_manifests(orders_enforced=False)
        changes = compute_contract_changes(previous, current)
        assert changes.changed_models == {"model.pkg.orders"}
        assert changes.changed_columns == {}

    def test_model_level_constraint_change_is_model_level(self):
        previous, current = build_manifests(
            orders_model_constraints=[
                ModelLevelConstraint(type=ConstraintType.check, expression="amount > 0")
            ]
        )
        changes = compute_contract_changes(previous, current)
        assert changes.changed_models == {"model.pkg.orders"}

    def test_unenforced_in_both_versions_is_ignored(self):
        previous, current = build_manifests(
            orders_columns={**ORDERS_COLUMNS, "amount": "numeric"},
            orders_enforced=False,
        )
        # previous manifest also unenforced
        previous.nodes["model.pkg.orders"].contract = Contract(enforced=False)
        changes = compute_contract_changes(previous, current)
        assert changes.changed_columns == {}
        assert changes.changed_models == set()

    def test_model_missing_from_either_manifest_is_ignored(self):
        previous, current = build_manifests(orders_columns={**ORDERS_COLUMNS, "amount": "numeric"})
        del previous.nodes["model.pkg.orders"]
        changes = compute_contract_changes(previous, current)
        assert changes.changed_columns == {}
        assert changes.changed_models == set()

    def test_equal_checksums_skip_comparison(self):
        # the checksum fast path trusts identical checksums
        previous, current = build_manifests(
            orders_columns={**ORDERS_COLUMNS, "amount": "numeric"},
            orders_checksum="abc123",
        )
        previous.nodes["model.pkg.orders"].contract.checksum = "abc123"
        changes = compute_contract_changes(previous, current)
        assert changes.changed_columns == {}
        assert changes.changed_models == set()

    def test_same_name_models_compared_separately(self):
        previous, current = build_manifests(orders_columns={**ORDERS_COLUMNS, "amount": "numeric"})
        # a same-named model in another package, unchanged
        other_orders_v1 = make_contracted_model("other", "orders", ORDERS_COLUMNS)
        other_orders = make_contracted_model("other", "orders", ORDERS_COLUMNS)
        previous.nodes[other_orders_v1.unique_id] = other_orders_v1
        current.nodes[other_orders.unique_id] = other_orders
        changes = compute_contract_changes(previous, current)
        assert changes.changed_columns == {"model.pkg.orders": {"amount"}}
        assert "model.other.orders" not in changes.changed_columns

    def test_model_versions_compared_separately(self):
        previous, current = build_manifests(orders_columns={**ORDERS_COLUMNS, "amount": "numeric"})
        # v2 of orders exists in both manifests, unchanged; v1 changed
        orders_v2_old = make_model("pkg", "orders", "select 1", version=2)
        orders_v2_old.columns = {
            name: ColumnInfo(name=name, data_type=data_type)
            for name, data_type in ORDERS_COLUMNS.items()
        }
        orders_v2_old.contract = Contract(enforced=True)
        orders_v2_new = copy.deepcopy(orders_v2_old)
        previous.nodes[orders_v2_old.unique_id] = orders_v2_old
        current.nodes[orders_v2_new.unique_id] = orders_v2_new
        changes = compute_contract_changes(previous, current)
        assert changes.changed_columns == {"model.pkg.orders": {"amount"}}
        assert "model.pkg.orders.v2" not in changes.changed_columns


class TestSelectContractDataTests:
    def test_column_change_selects_only_tests_reading_that_column(self):
        previous, current = build_manifests(
            orders_columns={**ORDERS_COLUMNS, "amount": "decimal(10,2)"}
        )
        selected = select_contract_data_tests(previous, current)
        assert selected == {
            uid("not_null_orders_amount"),
            # unclear tests attached to orders / downstream of orders
            uid("summary_positive"),
            uid("downstream_check"),
        }

    def test_relationships_selected_from_either_side(self):
        # foreign key side changes
        previous, current = build_manifests(
            orders_columns={**ORDERS_COLUMNS, "customer_id": "bigint"}
        )
        selected = select_contract_data_tests(previous, current)
        assert uid("relationships_orders_customer_id") in selected
        assert uid("not_null_orders_amount") not in selected

        # referenced side changes
        previous, current = build_manifests(
            customers_columns={**CUSTOMERS_COLUMNS, "id": "bigint"}
        )
        selected = select_contract_data_tests(previous, current)
        assert selected == {
            uid("relationships_orders_customer_id"),
            uid("not_null_customers_id"),
            uid("unique_customers_id"),
        }

    def test_where_clause_makes_test_conservative(self):
        previous, current = build_manifests(
            orders_columns={**ORDERS_COLUMNS, "amount": "decimal(10,2)"}
        )
        where_test = make_not_null(
            "pkg", current.nodes["model.pkg.orders"], "status", where="order_id > 0"
        )
        current.nodes[where_test.unique_id] = where_test
        current.build_parent_and_child_maps()
        selected = select_contract_data_tests(previous, current)
        assert uid("not_null_orders_status") in selected

    def test_fail_calc_makes_test_conservative(self):
        previous, current = build_manifests(
            orders_columns={**ORDERS_COLUMNS, "amount": "decimal(10,2)"}
        )
        test = make_not_null(
            "pkg", current.nodes["model.pkg.orders"], "status", fail_calc="count(distinct id)"
        )
        current.nodes[test.unique_id] = test
        current.build_parent_and_child_maps()
        selected = select_contract_data_tests(previous, current)
        assert uid("not_null_orders_status") in selected

    def test_macro_override_makes_builtin_conservative(self):
        previous, current = build_manifests(
            orders_columns={**ORDERS_COLUMNS, "amount": "decimal(10,2)"},
            extra_current_macros=[make_macro("pkg", "test_not_null", "select 1")],
        )
        selected = select_contract_data_tests(previous, current)
        # every not_null on orders is now selected, even on unchanged columns
        assert uid("not_null_orders_amount") in selected
        assert uid("not_null_orders_status") in selected
        # unique is not overridden and order_id did not change
        assert uid("unique_orders_order_id") not in selected
        # relationships is not overridden and customer_id did not change
        assert uid("relationships_orders_customer_id") not in selected
        # unclear not_nulls downstream of orders are kept
        assert uid("not_null_order_summary_total") in selected
        # independent branch is still untouched
        assert uid("not_null_independent_model_x") not in selected

    def test_adapter_dispatch_override_makes_builtin_conservative(self):
        previous, current = build_manifests(
            orders_columns={**ORDERS_COLUMNS, "amount": "decimal(10,2)"},
            extra_current_macros=[make_macro("pkg", "postgres__test_unique", "select 1")],
        )
        selected = select_contract_data_tests(previous, current)
        assert uid("unique_orders_order_id") in selected
        assert uid("not_null_orders_amount") in selected
        assert uid("not_null_orders_status") not in selected

    def test_adapter_shipped_macros_are_trusted(self):
        # macros shipped by the adapter package itself are not overrides
        previous, current = build_manifests(
            orders_columns={**ORDERS_COLUMNS, "amount": "decimal(10,2)"},
            extra_current_macros=[
                make_macro("dbt_postgres", "postgres__test_not_null", "select 1")
            ],
        )
        selected = select_contract_data_tests(previous, current)
        assert uid("not_null_orders_status") not in selected

    def test_custom_generic_test_is_conservative(self):
        previous, current = build_manifests(
            orders_columns={**ORDERS_COLUMNS, "amount": "decimal(10,2)"},
        )
        custom = make_custom_generic("pkg", current.nodes["model.pkg.orders"], "status")
        current.nodes[custom.unique_id] = custom
        current.build_parent_and_child_maps()
        selected = select_contract_data_tests(previous, current)
        # custom generic test on the changed model is kept even though only
        # amount changed and the test is declared on status
        assert uid("positive_value_orders_status") in selected

    def test_singular_tests_follow_downstream(self):
        previous, current = build_manifests(
            orders_columns={**ORDERS_COLUMNS, "amount": "decimal(10,2)"}
        )
        selected = select_contract_data_tests(previous, current)
        # transitive downstream of orders
        assert uid("summary_positive") in selected
        assert uid("downstream_check") in selected
        # independent branch
        assert uid("independent_check") not in selected

    def test_clear_tests_downstream_are_not_selected(self):
        previous, current = build_manifests(
            orders_columns={**ORDERS_COLUMNS, "amount": "decimal(10,2)"}
        )
        selected = select_contract_data_tests(previous, current)
        # not_null on order_summary.total reads an unchanged column of an
        # unchanged model, so it is provably unrelated
        assert uid("not_null_order_summary_total") not in selected

    def test_model_level_change_selects_all_tests_on_model(self):
        previous, current = build_manifests(orders_enforced=False)
        selected = select_contract_data_tests(previous, current)
        for test_id in (
            uid("not_null_orders_amount"),
            uid("not_null_orders_status"),
            uid("unique_orders_order_id"),
            uid("relationships_orders_customer_id"),
            uid("summary_positive"),
            uid("downstream_check"),
        ):
            assert test_id in selected
        assert uid("not_null_customers_name") not in selected
        assert uid("not_null_independent_model_x") not in selected

    def test_no_contract_changes_selects_nothing(self):
        previous, current = build_manifests()
        assert select_contract_data_tests(previous, current) == set()

    def test_expression_column_argument_is_conservative(self):
        previous, current = build_manifests(
            orders_columns={**ORDERS_COLUMNS, "amount": "decimal(10,2)"}
        )
        orders = current.nodes["model.pkg.orders"]
        test = make_generic_test(
            "pkg", "not_null", orders, {}, column_name="amount > 0", refs=[orders]
        )
        test.attached_node = orders.unique_id
        current.nodes[test.unique_id] = test
        current.build_parent_and_child_maps()
        selected = select_contract_data_tests(previous, current)
        # an expression instead of a plain column cannot be attributed, so the
        # test falls back to the conservative dependency-graph rule
        assert test.unique_id in selected


class TestStateSelectorMethod:
    def test_selector_returns_selected_tests(self):
        previous, current = build_manifests(
            orders_columns={**ORDERS_COLUMNS, "amount": "decimal(10,2)"}
        )
        previous_state = previous_state_from(previous)
        selected = search(current, previous_state, "modified.contract.data_tests")
        assert selected == {
            uid("not_null_orders_amount"),
            uid("summary_positive"),
            uid("downstream_check"),
        }

    def test_selector_requires_state_manifest(self):
        _, current = build_manifests()
        previous_state = previous_state_from(make_manifest())
        previous_state.manifest = None
        with pytest.raises(Exception) as exc:
            search(current, previous_state, "modified.contract.data_tests")
        assert "no comparison manifest" in str(exc.value)

    def test_selector_result_is_cached(self):
        previous, current = build_manifests(
            orders_columns={**ORDERS_COLUMNS, "amount": "decimal(10,2)"}
        )
        previous_state = previous_state_from(previous)
        methods = MethodManager(current, previous_state)
        method = methods.get_method("state", [])
        included = set(current.nodes)
        first = set(method.search(included, "modified.contract.data_tests"))
        # the analysis ran once and the result is stored on the method
        assert method._contract_data_test_selection == first
        # subsequent checks reuse the stored set, not the manifests
        current.nodes.clear()
        node = mock.Mock(unique_id="test.pkg.not_null_orders_amount")
        assert method.check_modified_contract_data_tests(None, node)
