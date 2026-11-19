"""Unit tests for the scoped hard-delete internal package registration."""

from dbt.adapters.factory import FACTORY, get_adapter_package_names
from dbt.snapshot_scope.include import PACKAGE_PATH, PROJECT_NAME
from dbt.snapshot_scope._register import register_snapshot_scope_package


class TestSnapshotScopeRegistration:
    def test_package_path_registered(self):
        register_snapshot_scope_package()
        assert PROJECT_NAME in FACTORY.packages
        assert str(FACTORY.packages[PROJECT_NAME]) == PACKAGE_PATH

    def test_package_name_prepended_and_idempotent(self):
        register_snapshot_scope_package()
        # With no adapter plugin loaded the underlying factory returns just
        # the global 'dbt' package; our wrapper must still prepend itself.
        names = get_adapter_package_names(None)
        assert names[0] == PROJECT_NAME
        assert names[-1] == "dbt"
        # Re-registering must not produce a duplicate entry
        register_snapshot_scope_package()
        names_again = get_adapter_package_names(None)
        assert names_again.count(PROJECT_NAME) == 1
        assert names_again == names

    def test_wrapper_flag_guards_double_wrapping(self):
        register_snapshot_scope_package()
        assert getattr(FACTORY, "_snapshot_scope_wrapped", False) is True
        wrapped = FACTORY.get_adapter_package_names
        register_snapshot_scope_package()
        # The same wrapper object is retained rather than re-wrapped
        assert FACTORY.get_adapter_package_names is wrapped
