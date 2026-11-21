"""Regression tests for unit test fixture edits under partial parsing.

Editing a CSV fixture in ``tests/fixtures`` changes the rows baked into every
unit test definition that reads it. These tests exercise the partial parsing
path end to end: a warm parse must update exactly the affected definitions,
must not fall back to a full reparse, and must produce the same definitions as
parsing from scratch.

The project used here mirrors the reported reproduction:

* ``base`` returns a single column ``id`` with value 1.
* ``sales`` is versioned (1 and 2); each version selects from ``base``.
* One ``echo`` unit test reads ``ref('base')`` through the shared fixture
  ``shared.csv`` and expects ``id = 1``.

Run with a local DuckDB project:

    python -m pytest tests/functional/unit_testing/test_ut_fixture_partial_parsing.py
"""

import os
from typing import Any, Dict, List, Optional

import pytest

from dbt.tests.util import get_manifest, run_dbt, run_dbt_and_capture, write_file

# These tests run a real local project and need a working adapter. DuckDB keeps
# them self-contained (no server); dbt-core's own functional CI runs Postgres,
# so skip cleanly there instead of failing for a missing optional dependency.
pytest.importorskip("dbt.adapters.duckdb")

# ---------------------------------------------------------------------------
# Project file contents
# ---------------------------------------------------------------------------

BASE_SQL = "select 1 as id\n"
SALES_V1_SQL = "select * from {{ ref('base') }}\n"
SALES_V2_SQL = "select * from {{ ref('base') }}\n"

SALES_YML = """
version: 2
models:
  - name: base
  - name: sales
    latest_version: 2
    versions:
      - v: 1
        defined_in: sales_v1
      - v: 2
        defined_in: sales_v2
"""

SALES_YML_V3 = """
version: 2
models:
  - name: base
  - name: sales
    latest_version: 3
    versions:
      - v: 1
        defined_in: sales_v1
      - v: 2
        defined_in: sales_v2
      - v: 3
        defined_in: sales_v3
"""


def _unit_yml(
    name: str = "echo",
    model: str = "sales",
    fixture: str = "shared",
    expect_fixture: Optional[str] = None,
    expect_rows: str = "id\n1",
    versions: Optional[Dict[str, List[int]]] = None,
) -> str:
    """Build a single unit test yaml file."""
    versions_block = ""
    if versions is not None:
        lines = ["    versions:"]
        for key, values in versions.items():
            lines.append(f"      {key}:")
            for value in values:
                lines.append(f"        - {value}")
        versions_block = "\n".join(lines) + "\n"

    if expect_fixture:
        expect_block = f"    expect:\n      format: csv\n      fixture: {expect_fixture}\n"
    else:
        expect_lines = "\n".join(f"        {line}" for line in expect_rows.splitlines())
        expect_block = f"    expect:\n      format: csv\n      rows: |\n{expect_lines}\n"

    return (
        "version: 2\n"
        "unit_tests:\n"
        f"  - name: {name}\n"
        f"    model: {model}\n"
        f"{versions_block}"
        "    given:\n"
        "      - input: ref('base')\n"
        "        format: csv\n"
        f"        fixture: {fixture}\n"
        f"{expect_block}"
    )


def _csv(value: int) -> str:
    """Single-column CSV fixture with one row."""
    return f"id\n{value}\n"


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------


def _project(manifest) -> Dict[str, Any]:
    """Project the manifest fields that a fixture edit can legally change."""
    unit_tests = {}
    for unique_id, definition in manifest.unit_tests.items():
        unit_tests[unique_id] = {
            "version": definition.version,
            "versions": definition.versions.to_dict() if definition.versions else None,
            "depends_on": list(definition.depends_on.nodes),
            "given": [given.to_dict() for given in definition.given],
            "expect": definition.expect.to_dict(),
            "checksum": definition.checksum,
        }
    models = {}
    for unique_id, node in manifest.nodes.items():
        if node.resource_type != "model":
            continue
        models[unique_id] = {
            "version": getattr(node, "version", None),
            "columns": sorted(node.columns.keys()),
            "contract": node.config.contract.to_dict() if node.config.contract else None,
        }
    return {"unit_tests": unit_tests, "models": models}


def _fixture_index(manifest) -> Dict[str, List[str]]:
    index = {}
    for source_file in manifest.files.values():
        if getattr(source_file, "parse_file_type", None) == "fixture":
            # Keyed by the project-relative path used in yaml, e.g. tests/fixtures/x.csv.
            index[source_file.original_file_path] = list(source_file.unit_tests or [])
    return index


def _parse_debug() -> str:
    """Run ``dbt parse --debug`` and return the captured log output."""
    _, output = run_dbt_and_capture(["parse", "--debug"])
    return output


def _assert_no_fallback(output: str) -> None:
    assert "Switching to full reparse" not in output
    assert "Unable to do partial parsing" not in output


def _force_full_parse(project) -> None:
    """Remove the cache so the next parse starts from scratch."""
    cache = os.path.join(project.project_root, "target", "partial_parse.msgpack")
    if os.path.exists(cache):
        os.remove(cache)
    run_dbt(["parse"])


def _write_fixture(project, name: str, value: int) -> None:
    write_file(_csv(value), project.project_root, "tests", "fixtures", f"{name}.csv")


# Every test class in this module runs on its own DuckDB database file.
@pytest.fixture(scope="class")
def dbt_profile_target(tmp_path_factory):
    db_dir = tmp_path_factory.mktemp("duckdb")
    return {
        "type": "duckdb",
        "path": str(db_dir / "local.duckdb"),
        "threads": 1,
    }


# ---------------------------------------------------------------------------
# The reported reproduction
# ---------------------------------------------------------------------------


class TestFixtureEditPartialParsing:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "base.sql": BASE_SQL,
            "sales_v1.sql": SALES_V1_SQL,
            "sales_v2.sql": SALES_V2_SQL,
            "sales.yml": SALES_YML,
            "unit_sales.yml": _unit_yml(),
        }

    @pytest.fixture(scope="class")
    def tests(self):
        return {"fixtures": {"shared.csv": _csv(1)}}

    def test_warm_parse_matches_full_parse(self, project):
        _write_fixture(project, "shared", 1)
        run_dbt(["parse"])

        before = _project(get_manifest(project.project_root))
        expected_ids = {
            "unit_test.test.sales.echo_v1",
            "unit_test.test.sales.echo_v2",
        }
        assert set(before["unit_tests"]) == expected_ids

        _write_fixture(project, "shared", 2)
        output = _parse_debug()
        _assert_no_fallback(output)

        after = _project(get_manifest(project.project_root))
        assert set(after["unit_tests"]) == expected_ids
        for unique_id in expected_ids:
            assert after["unit_tests"][unique_id]["given"][0]["rows"] == [{"id": "2"}]
            # The inline expect is unaffected.
            assert after["unit_tests"][unique_id]["expect"]["rows"] == [{"id": "1"}]

        _force_full_parse(project)
        assert _project(get_manifest(project.project_root)) == after

    def test_fixture_index_registers_expanded_versions(self, project):
        _write_fixture(project, "shared", 3)
        run_dbt(["parse"])
        manifest = get_manifest(project.project_root)
        assert _fixture_index(manifest)["tests/fixtures/shared.csv"] == [
            "unit_test.test.sales.echo_v1",
            "unit_test.test.sales.echo_v2",
        ]

    def test_input_fixture_drives_pass_fail(self, project):
        _write_fixture(project, "shared", 1)
        run_dbt(["build"])
        results = run_dbt(["test"])
        assert all(result.status == "pass" for result in results)

        # Changing only the input fixture must fail the tests...
        _write_fixture(project, "shared", 2)
        results = run_dbt(["test"], expect_pass=False)
        assert len(results) == 2
        assert all(result.status == "fail" for result in results)

        # ...and reverting it must pass again through partial parsing.
        _write_fixture(project, "shared", 1)
        results = run_dbt(["test"])
        assert all(result.status == "pass" for result in results)


# ---------------------------------------------------------------------------
# Input and expected output in separate files
# ---------------------------------------------------------------------------


class TestExpectOnlyFixtureEdit:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "base.sql": BASE_SQL,
            "sales.sql": SALES_V1_SQL,
            "unit_sales.yml": _unit_yml(
                model="sales", fixture="input_rows", expect_fixture="expected_rows"
            ),
        }

    @pytest.fixture(scope="class")
    def tests(self):
        return {"fixtures": {"input_rows.csv": _csv(1), "expected_rows.csv": _csv(1)}}

    def test_expect_only_change(self, project):
        run_dbt(["parse"])
        _write_fixture(project, "expected_rows", 2)
        output = _parse_debug()
        _assert_no_fallback(output)

        definition = get_manifest(project.project_root).unit_tests["unit_test.test.sales.echo"]
        assert definition.given[0].rows == [{"id": "1"}]
        assert definition.expect.rows == [{"id": "2"}]

    def test_expect_fixture_drives_pass_fail(self, project):
        _write_fixture(project, "input_rows", 1)
        _write_fixture(project, "expected_rows", 1)
        run_dbt(["run"])
        assert len(run_dbt(["test"])) == 1

        _write_fixture(project, "expected_rows", 2)
        results = run_dbt(["test"], expect_pass=False)
        assert [result.status for result in results] == ["fail"]

        _write_fixture(project, "expected_rows", 1)
        assert len(run_dbt(["test"])) == 1

    def test_both_fixtures_changed_in_one_parse(self, project):
        _write_fixture(project, "input_rows", 4)
        _write_fixture(project, "expected_rows", 5)
        output = _parse_debug()
        _assert_no_fallback(output)

        definition = get_manifest(project.project_root).unit_tests["unit_test.test.sales.echo"]
        assert definition.given[0].rows == [{"id": "4"}]
        assert definition.expect.rows == [{"id": "5"}]

        warm = _project(get_manifest(project.project_root))
        _force_full_parse(project)
        assert _project(get_manifest(project.project_root)) == warm


# ---------------------------------------------------------------------------
# One fixture used as both input and expected output
# ---------------------------------------------------------------------------


class TestSameFixtureAsInputAndExpect:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "base.sql": BASE_SQL,
            "sales.sql": SALES_V1_SQL,
            "unit_sales.yml": _unit_yml(model="sales", fixture="shared", expect_fixture="shared"),
        }

    @pytest.fixture(scope="class")
    def tests(self):
        return {"fixtures": {"shared.csv": _csv(1)}}

    def test_shared_fixture_updates_both_sides_once(self, project):
        run_dbt(["parse"])
        _write_fixture(project, "shared", 2)
        output = _parse_debug()
        _assert_no_fallback(output)

        manifest = get_manifest(project.project_root)
        definition = manifest.unit_tests["unit_test.test.sales.echo"]
        assert definition.given[0].rows == [{"id": "2"}]
        assert definition.expect.rows == [{"id": "2"}]
        # The double reference is recorded exactly once.
        assert _fixture_index(manifest)["tests/fixtures/shared.csv"] == [
            "unit_test.test.sales.echo"
        ]

    def test_shared_fixture_run(self, project):
        # The model is the identity, so this test passes for either fixture content,
        # but editing the shared file must still run cleanly through partial parsing.
        _write_fixture(project, "shared", 1)
        run_dbt(["run"])
        assert len(run_dbt(["test"])) == 1

        _write_fixture(project, "shared", 2)
        output = _parse_debug()
        _assert_no_fallback(output)
        results = run_dbt(["test"])
        assert [result.status for result in results] == ["pass"]


# ---------------------------------------------------------------------------
# Version include / exclude
# ---------------------------------------------------------------------------


class TestVersionFiltersStillApply:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "base.sql": BASE_SQL,
            "sales_v1.sql": SALES_V1_SQL,
            "sales_v2.sql": SALES_V2_SQL,
            "sales.yml": SALES_YML,
            "unit_sales.yml": _unit_yml(versions={"include": [2]}),
        }

    @pytest.fixture(scope="class")
    def tests(self):
        return {"fixtures": {"shared.csv": _csv(1)}}

    def test_include_exclude_and_no_filter(self, project):
        run_dbt(["parse"])
        _write_fixture(project, "shared", 2)
        output = _parse_debug()
        _assert_no_fallback(output)
        manifest = get_manifest(project.project_root)
        assert set(manifest.unit_tests) == {"unit_test.test.sales.echo_v2"}
        assert manifest.unit_tests["unit_test.test.sales.echo_v2"].given[0].rows == [{"id": "2"}]

        # Switch to exclude, then edit the fixture again.
        write_file(
            _unit_yml(versions={"exclude": [1]}),
            project.project_root,
            "models",
            "unit_sales.yml",
        )
        run_dbt(["parse"])
        _write_fixture(project, "shared", 3)
        output = _parse_debug()
        _assert_no_fallback(output)
        manifest = get_manifest(project.project_root)
        assert set(manifest.unit_tests) == {"unit_test.test.sales.echo_v2"}
        assert manifest.unit_tests["unit_test.test.sales.echo_v2"].given[0].rows == [{"id": "3"}]

        # Remove the filter; both versions come back with fresh rows.
        write_file(_unit_yml(), project.project_root, "models", "unit_sales.yml")
        run_dbt(["parse"])
        _write_fixture(project, "shared", 4)
        output = _parse_debug()
        _assert_no_fallback(output)
        manifest = get_manifest(project.project_root)
        assert set(manifest.unit_tests) == {
            "unit_test.test.sales.echo_v1",
            "unit_test.test.sales.echo_v2",
        }
        for definition in manifest.unit_tests.values():
            assert definition.given[0].rows == [{"id": "4"}]


# ---------------------------------------------------------------------------
# Cache reuse across processes and multiple rounds
# ---------------------------------------------------------------------------


class TestCrossProcessReuse:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "base.sql": BASE_SQL,
            "sales_v1.sql": SALES_V1_SQL,
            "sales_v2.sql": SALES_V2_SQL,
            "sales.yml": SALES_YML,
            "unit_sales.yml": _unit_yml(),
        }

    @pytest.fixture(scope="class")
    def tests(self):
        return {"fixtures": {"shared.csv": _csv(1)}}

    def test_multiple_rounds_in_fresh_invocations(self, project):
        run_dbt(["parse"])
        for value in (2, 3, 4):
            _write_fixture(project, "shared", value)
            # Every run_dbt call is a fresh invocation with a new loader.
            output = _parse_debug()
            _assert_no_fallback(output)
            manifest = get_manifest(project.project_root)
            for definition in manifest.unit_tests.values():
                assert definition.given[0].rows == [{"id": str(value)}]
            # The saved indexes do not grow across rounds.
            assert _fixture_index(manifest)["tests/fixtures/shared.csv"] == [
                "unit_test.test.sales.echo_v1",
                "unit_test.test.sales.echo_v2",
            ]


# ---------------------------------------------------------------------------
# Project evolution: rename/delete tests, add/remove versions, swap fixtures
# ---------------------------------------------------------------------------


class TestProjectEvolution:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "base.sql": BASE_SQL,
            "sales_v1.sql": SALES_V1_SQL,
            "sales_v2.sql": SALES_V2_SQL,
            "sales.yml": SALES_YML,
            "unit_sales.yml": _unit_yml(),
        }

    @pytest.fixture(scope="class")
    def tests(self):
        return {"fixtures": {"shared.csv": _csv(1), "other.csv": _csv(9)}}

    def test_rename_and_delete_then_edit_old_fixture(self, project):
        run_dbt(["parse"])
        write_file(_unit_yml(name="renamed"), project.project_root, "models", "unit_sales.yml")
        run_dbt(["parse"])
        output = _parse_debug()
        _assert_no_fallback(output)
        manifest = get_manifest(project.project_root)
        assert set(manifest.unit_tests) == {
            "unit_test.test.sales.renamed_v1",
            "unit_test.test.sales.renamed_v2",
        }

        # Delete it, then change the old fixture: the removed test must not return.
        write_file("version: 2\n", project.project_root, "models", "unit_sales.yml")
        run_dbt(["parse"])
        _write_fixture(project, "shared", 7)
        output = _parse_debug()
        _assert_no_fallback(output)
        manifest = get_manifest(project.project_root)
        assert manifest.unit_tests == {}
        assert _fixture_index(manifest)["tests/fixtures/shared.csv"] == []

    def test_add_and_remove_model_version(self, project):
        write_file(_unit_yml(), project.project_root, "models", "unit_sales.yml")
        _write_fixture(project, "shared", 1)
        run_dbt(["parse"])

        # Add version 3 to the model.
        write_file(SALES_YML_V3, project.project_root, "models", "sales.yml")
        write_file(SALES_V2_SQL, project.project_root, "models", "sales_v3.sql")
        output = _parse_debug()
        _assert_no_fallback(output)
        assert set(get_manifest(project.project_root).unit_tests) == {
            "unit_test.test.sales.echo_v1",
            "unit_test.test.sales.echo_v2",
            "unit_test.test.sales.echo_v3",
        }

        _write_fixture(project, "shared", 8)
        output = _parse_debug()
        _assert_no_fallback(output)
        manifest = get_manifest(project.project_root)
        assert set(manifest.unit_tests) == {
            "unit_test.test.sales.echo_v1",
            "unit_test.test.sales.echo_v2",
            "unit_test.test.sales.echo_v3",
        }
        for definition in manifest.unit_tests.values():
            assert definition.given[0].rows == [{"id": "8"}]

        # Remove version 3, then edit the fixture: it must stay removed.
        write_file(SALES_YML, project.project_root, "models", "sales.yml")
        os.remove(os.path.join(project.project_root, "models", "sales_v3.sql"))
        run_dbt(["parse"])
        _write_fixture(project, "shared", 9)
        output = _parse_debug()
        _assert_no_fallback(output)
        assert set(get_manifest(project.project_root).unit_tests) == {
            "unit_test.test.sales.echo_v1",
            "unit_test.test.sales.echo_v2",
        }

    def test_swap_fixture_then_edit_old_one(self, project):
        write_file(_unit_yml(), project.project_root, "models", "unit_sales.yml")
        _write_fixture(project, "shared", 1)
        _write_fixture(project, "other", 9)
        run_dbt(["parse"])

        # Switch the test to another fixture.
        write_file(_unit_yml(fixture="other"), project.project_root, "models", "unit_sales.yml")
        run_dbt(["parse"])
        manifest = get_manifest(project.project_root)
        assert _fixture_index(manifest)["tests/fixtures/shared.csv"] == []
        assert _fixture_index(manifest)["tests/fixtures/other.csv"] == [
            "unit_test.test.sales.echo_v1",
            "unit_test.test.sales.echo_v2",
        ]

        # Editing the old fixture changes nothing.
        _write_fixture(project, "shared", 2)
        output = _parse_debug()
        _assert_no_fallback(output)
        for definition in get_manifest(project.project_root).unit_tests.values():
            assert definition.given[0].rows == [{"id": "9"}]

    def test_deleted_fixture_reports_fixture_and_test(self, project):
        write_file(_unit_yml(), project.project_root, "models", "unit_sales.yml")
        _write_fixture(project, "shared", 1)
        run_dbt(["parse"])

        os.remove(os.path.join(project.project_root, "tests", "fixtures", "shared.csv"))
        with pytest.raises(
            Exception,
            match=r"File not found for fixture 'shared'.*unit_test.test.sales.echo",
        ):
            run_dbt(["parse"])


# ---------------------------------------------------------------------------
# Column contract selection is unaffected
# ---------------------------------------------------------------------------


class TestColumnContractSelection:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "base.sql": BASE_SQL,
            "sales_v1.sql": "select id, 1 as extra from {{ ref('base') }} b\n",
            "sales_v2.sql": SALES_V2_SQL,
            "sales.yml": """
version: 2
models:
  - name: base
  - name: sales
    latest_version: 2
    config:
      contract:
        enforced: true
    columns:
      - name: id
        data_type: integer
      - name: extra
        data_type: integer
    versions:
      - v: 1
        defined_in: sales_v1
      - v: 2
        defined_in: sales_v2
        columns:
          - include: all
            exclude:
              - extra
""",
            "unit_sales.yml": _unit_yml(),
        }

    @pytest.fixture(scope="class")
    def tests(self):
        return {"fixtures": {"shared.csv": _csv(1)}}

    def test_columns_per_version_survive_fixture_edit(self, project):
        run_dbt(["parse"])
        expected_columns = {
            "model.test.sales.v1": ["extra", "id"],
            "model.test.sales.v2": ["id"],
        }
        manifest = get_manifest(project.project_root)
        for model_id, columns in expected_columns.items():
            assert sorted(manifest.nodes[model_id].columns) == columns

        _write_fixture(project, "shared", 2)
        output = _parse_debug()
        _assert_no_fallback(output)
        manifest = get_manifest(project.project_root)
        for model_id, columns in expected_columns.items():
            assert sorted(manifest.nodes[model_id].columns) == columns
            assert manifest.nodes[model_id].config.contract.enforced
