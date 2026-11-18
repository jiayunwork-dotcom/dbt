"""Column-level contract change detection and contract-aware data test selection.

This module backs the ``state:modified.contract.data_tests`` selector. Given the
manifest saved in ``--state`` and the manifest of the current project, it works
out which *data tests* need to run because a model contract changed.

Two kinds of contract changes are detected for models that exist (and are
enabled) in both manifests:

* column-level changes on models with an enforced contract: a column was added
  or removed, its data type changed, or its column-level constraints changed.
* model-level changes: the contract was toggled on/off, or the model-level
  constraints changed. These are treated as "every column changed".

Changes that do not alter the contract (descriptions, YAML column ordering,
model SQL, tags, meta, ...) select nothing.

Data tests are then classified by what they read:

* "Column-clear" tests are the built-in ``not_null``, ``unique`` and
  ``relationships`` generic tests with no ``where`` config, no non-default
  ``fail_calc``, no macro override, and plain column arguments. Their read set
  is exactly known, so they are selected only if one of the columns they read
  changed. ``relationships`` reads a column on each side, so a change on either
  end selects it.
* Every other test (singular tests, custom generic tests, built-ins with a
  ``where`` clause / overridden macro / expression arguments) has an unknown
  read set. These are selected conservatively: if any changed model is an
  ancestor of the test in the current dependency graph, the test cannot be
  proven unrelated and is kept. Tests on fully independent branches are not
  selected.

All analysis happens lazily at selection time and is a pure function of the two
manifests, so results are identical whether the current manifest came from a
partial parse or a full parse.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from dbt.contracts.graph.manifest import Manifest
from dbt.contracts.graph.nodes import GenericTestNode, ModelNode
from dbt.node_types import NodeType
from dbt_common.contracts.constraints import ColumnLevelConstraint

# Generic tests shipped in dbt's global project whose column reads are fully
# described by their column_name / field arguments.
BUILTIN_COLUMN_TESTS = frozenset({"not_null", "unique", "relationships"})

# Package that ships the built-in generic tests.
_DBT_PACKAGE = "dbt"

# Arguments that are a plain column reference, not a SQL expression.
_PLAIN_COLUMN_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Macros named like a built-in test implementation: "test_not_null",
# "default__test_not_null", "duckdb__test_not_null", ...
_TEST_MACRO_PATTERN = re.compile(r"^(?:.+__)?test_(not_null|unique|relationships)$")

# fail_calc values that cannot read extra columns.
_DEFAULT_FAIL_CALC = "count(*)"


@dataclass
class ContractChanges:
    """The contract diff between two manifests.

    changed_columns: model unique_id -> names of columns whose contract changed
        (added, removed, data type change, or column constraint change).
    changed_models: model unique_ids whose change cannot be attributed to
        individual columns (contract toggled, or model-level constraints
        changed). Every column of these models counts as changed.
    """

    changed_columns: Dict[str, Set[str]] = field(default_factory=dict)
    changed_models: Set[str] = field(default_factory=set)

    def models_with_changes(self) -> Set[str]:
        return set(self.changed_columns) | self.changed_models


def _normalize_data_type(data_type: Optional[str]) -> Optional[str]:
    """Normalize a declared data type for comparison.

    Case and whitespace differences in the declaration do not constitute a
    contract change.
    """
    if data_type is None:
        return None
    return re.sub(r"\s+", " ", data_type.strip()).lower()


def _constraint_signature(constraints: Optional[Sequence[ColumnLevelConstraint]]) -> List[str]:
    """An order-insensitive signature for a list of constraints."""
    signature = []
    for constraint in constraints or []:
        signature.append(
            json.dumps(constraint.to_dict(omit_none=True), sort_keys=True, default=str)
        )
    return sorted(signature)


def _changed_column_names(old: ModelNode, new: ModelNode) -> Set[str]:
    """Column names whose contract-relevant declaration changed between two
    versions of the same model. Both models must have contracts enforced."""
    changed: Set[str] = set()
    for name in old.columns.keys() | new.columns.keys():
        old_column = old.columns.get(name)
        new_column = new.columns.get(name)
        if old_column is None or new_column is None:
            # column added or removed
            changed.add(name)
            continue
        if _normalize_data_type(old_column.data_type) != _normalize_data_type(
            new_column.data_type
        ):
            changed.add(name)
            continue
        if _constraint_signature(old_column.constraints) != _constraint_signature(
            new_column.constraints
        ):
            changed.add(name)
    return changed


def compute_contract_changes(previous: Manifest, current: Manifest) -> ContractChanges:
    """Diff the contracts of models present and enabled in both manifests.

    Models are matched by unique_id, so same-named models from different
    packages, and different versions of the same model, are each compared
    separately. Models missing from either manifest are out of scope for this
    method (they are covered by state:new / state:modified).
    """
    changes = ContractChanges()
    for unique_id, new_node in current.nodes.items():
        if not isinstance(new_node, ModelNode):
            continue
        old_node = previous.nodes.get(unique_id)
        if old_node is None or not isinstance(old_node, ModelNode):
            continue

        old_enforced = old_node.contract.enforced
        new_enforced = new_node.contract.enforced
        if not old_enforced and not new_enforced:
            # no contract in either version: column edits are not contract changes
            continue
        if old_enforced != new_enforced:
            # contract toggled: treat the whole model as changed
            changes.changed_models.add(unique_id)
            continue

        # Both contracts are enforced. The checksum covers columns and
        # constraints, so identical checksums mean no contract change.
        if (
            old_node.contract.checksum is not None
            and new_node.contract.checksum is not None
            and old_node.contract.checksum == new_node.contract.checksum
        ):
            continue

        # Model-level constraint changes are attributed to the whole model.
        if _constraint_signature(old_node.constraints) != _constraint_signature(
            new_node.constraints
        ):
            changes.changed_models.add(unique_id)
            continue

        changed_columns = _changed_column_names(old_node, new_node)
        if changed_columns:
            changes.changed_columns[unique_id] = changed_columns
    return changes


def _trusted_test_macro_packages(adapter_type: Optional[str]) -> Set[str]:
    """Packages whose built-in test macros are considered the standard
    implementation: dbt's global project and the adapter plugin itself."""
    packages = {_DBT_PACKAGE}
    if adapter_type:
        packages.add(f"{_DBT_PACKAGE}_{adapter_type}")
    return packages


def _overridden_builtin_tests(manifest: Manifest, adapter_type: Optional[str]) -> Set[str]:
    """Names of built-in column tests whose macro is overridden outside the
    trusted packages (project-level or dependency-level overrides, including
    adapter-dispatch overrides such as ``duckdb__test_not_null``)."""
    overridden: Set[str] = set()
    trusted = _trusted_test_macro_packages(adapter_type)
    for macro in manifest.macros.values():
        if macro.package_name in trusted:
            continue
        match = _TEST_MACRO_PATTERN.match(macro.name)
        if match:
            overridden.add(match.group(1))
    return overridden


def _is_plain_column_argument(value: Optional[str], declared_columns) -> bool:
    """True if a test argument is a plain column reference rather than a SQL
    expression. Declared column names always qualify."""
    if not value or not isinstance(value, str):
        return False
    if value in declared_columns:
        return True
    return bool(_PLAIN_COLUMN_PATTERN.match(value))


def _generic_test_column_reads(
    test: GenericTestNode,
    manifest: Manifest,
    overridden_tests: Set[str],
) -> Optional[Set[Tuple[str, str]]]:
    """The set of (model unique_id, column name) pairs a generic test is known
    to read, or None if the read set cannot be determined confidently."""
    metadata = test.test_metadata
    if metadata is None:
        return None
    name = metadata.name
    if name not in BUILTIN_COLUMN_TESTS:
        return None
    # "dbt.not_null" is still the built-in; any other namespace is not.
    if metadata.namespace not in (None, _DBT_PACKAGE):
        return None
    if name in overridden_tests:
        return None
    # A where clause or a custom fail_calc may read arbitrary columns.
    if test.config.where or metadata.kwargs.get("where"):
        return None
    if test.config.fail_calc and test.config.fail_calc != _DEFAULT_FAIL_CALC:
        return None

    attached_node = manifest.nodes.get(test.attached_node) if test.attached_node else None
    if attached_node is None or not isinstance(attached_node, ModelNode):
        return None

    column_name = metadata.kwargs.get("column_name") or test.column_name
    if not isinstance(column_name, str) or not _is_plain_column_argument(
        column_name, attached_node.columns
    ):
        return None

    reads: Set[Tuple[str, str]] = {(attached_node.unique_id, column_name)}

    if name == "relationships":
        field = metadata.kwargs.get("field")
        referenced = [
            unique_id
            for unique_id in test.depends_on.nodes
            if unique_id != attached_node.unique_id
            and isinstance(manifest.nodes.get(unique_id), ModelNode)
        ]
        if len(referenced) != 1:
            return None
        referenced_node = manifest.nodes[referenced[0]]
        if not isinstance(field, str) or not _is_plain_column_argument(
            field, referenced_node.columns
        ):
            return None
        reads.add((referenced_node.unique_id, field))

    return reads


def _descendant_test_ids(manifest: Manifest, model_ids: Set[str]) -> Set[str]:
    """All data test unique_ids reachable downstream of the given models in the
    current dependency graph."""
    child_map = manifest.child_map or {}
    test_ids: Set[str] = set()
    seen: Set[str] = set(model_ids)
    stack: List[str] = list(model_ids)
    while stack:
        unique_id = stack.pop()
        for child_id in child_map.get(unique_id, []):
            if child_id in seen:
                continue
            seen.add(child_id)
            stack.append(child_id)
            child = manifest.nodes.get(child_id)
            if child is not None and child.resource_type == NodeType.Test:
                test_ids.add(child_id)
    return test_ids


def select_contract_data_tests(previous: Manifest, current: Manifest) -> Set[str]:
    """Unique_ids of data tests that must run given the contract changes
    between the previous (state) and current manifests."""
    selected: Set[str] = set()
    changes = compute_contract_changes(previous, current)
    changed_models = changes.models_with_changes()
    if not changed_models:
        return selected

    adapter_type = current.metadata.adapter_type
    overridden_tests = _overridden_builtin_tests(current, adapter_type)

    unclear_test_ids: List[str] = []
    for unique_id, node in current.nodes.items():
        if node.resource_type != NodeType.Test:
            continue
        reads = None
        if isinstance(node, GenericTestNode):
            reads = _generic_test_column_reads(node, current, overridden_tests)
        if reads is None:
            # The read set of this test is unknown; handled by the
            # dependency-graph fallback below.
            unclear_test_ids.append(unique_id)
            continue
        for model_id, column_name in reads:
            if model_id in changes.changed_models:
                selected.add(unique_id)
                break
            if column_name in changes.changed_columns.get(model_id, set()):
                selected.add(unique_id)
                break

    if unclear_test_ids:
        if current.child_map is None:
            # the maps are built during parsing; rebuild defensively rather
            # than silently dropping downstream tests
            current.build_parent_and_child_maps()
        downstream_test_ids = _descendant_test_ids(current, changed_models)
        for unique_id in unclear_test_ids:
            if unique_id in downstream_test_ids:
                selected.add(unique_id)

    return selected
