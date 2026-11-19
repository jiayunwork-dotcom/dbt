"""Internal dbt-core package carrying the scoped hard-delete snapshot feature.

The global Jinja project itself (``dbt.include.global_project``) is shipped by
the ``dbt-adapters`` distribution in dbt 1.10.  To keep the feature entirely
inside dbt-core -- without asking users to add project macros and without
modifying an installed adapter -- dbt-core registers its own internal package
(see :mod:`dbt.snapshot_scope._register`) alongside the adapter packages.
"""
