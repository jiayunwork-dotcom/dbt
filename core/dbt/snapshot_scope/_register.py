"""Registration of dbt-core's snapshot-scope macro package.

In dbt 1.10 the global Jinja project (and therefore the default snapshot
materialization macros) is distributed inside ``dbt-adapters``.  dbt-core may
not modify an installed adapter, so this module registers a *separate*
internal package -- ``dbt_snapshot_scope``, shipped inside dbt-core itself --
with the adapter :class:`~dbt.adapters.factory.AdapterContainer`.

Two registrations are needed:

1. the include path is added to ``FACTORY.packages`` so
   ``get_include_paths`` can load the package's ``dbt_project.yml`` and
   macros as an internal project;
2. ``FACTORY.get_adapter_package_names`` is wrapped so the package name is
   treated as an internal macro namespace everywhere dbt-core asks for the
   internal package list (macro resolution, locality classification, partial
   parsing).

The package is prepended to the internal package list, ahead of the global
``dbt`` package. Internal macro namespaces merge in reverse order, so macros
defined here take precedence over same-named global macros, while everything
else remains untouched. The package defines no materializations, so the strict
``DuplicateMaterializationNameError`` rule for core materializations never
applies.

The registration happens at import time (dbt.config.profile imports this
module before the first adapter plugin loads), so ``dbt parse`` sees the
macros exactly like a normal CLI invocation does.
"""

from pathlib import Path

from dbt.adapters.factory import FACTORY

from dbt.snapshot_scope.include import PACKAGE_PATH, PROJECT_NAME


def register_snapshot_scope_package() -> None:
    """Register the dbt-core snapshot-scope include path and package name."""
    # Mapping assignment is idempotent: re-imports (common in the test suite)
    # simply overwrite the same path with an identical value.
    FACTORY.packages[PROJECT_NAME] = Path(PACKAGE_PATH)

    # Only wrap once, even if the module is re-imported after a factory reset.
    if getattr(FACTORY, "_snapshot_scope_wrapped", False):
        return

    original_get_package_names = FACTORY.get_adapter_package_names

    def get_adapter_package_names_with_scope(name):
        package_names = list(original_get_package_names(name))
        if PROJECT_NAME in package_names:
            package_names.remove(PROJECT_NAME)
        # Prepend: internal macro namespaces are merged in reverse order, so
        # the first package in this list overwrites same-named macros from the
        # packages that follow it (notably the global 'dbt' package).
        package_names.insert(0, PROJECT_NAME)
        return package_names

    FACTORY.get_adapter_package_names = get_adapter_package_names_with_scope
    FACTORY._snapshot_scope_wrapped = True


register_snapshot_scope_package()
