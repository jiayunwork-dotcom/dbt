"""Unit tests for the fixture -> unit test usage index used by partial parsing.

The end-to-end behaviour is covered in
``tests/functional/unit_testing/test_ut_fixture_partial_parsing.py``. These
tests pin down the bookkeeping itself: version-expanded definitions, tests
sharing one fixture, a test referencing the same fixture twice, and stale ids
left behind by earlier caches.
"""

import time
from copy import deepcopy

import pytest

from dbt.contracts.files import (
    FileHash,
    FilePath,
    FixtureSourceFile,
    ParseFileType,
    SchemaSourceFile,
)
from dbt.contracts.graph.manifest import Manifest
from dbt.contracts.graph.nodes import DependsOn, UnitTestDefinition
from dbt.contracts.graph.unparsed import UnitTestInputFixture, UnitTestOutputFixture
from dbt.node_types import NodeType
from dbt.parser.partial import PartialParsing
from dbt.tests.util import safe_set_invocation_context

PROJECT = "pkg"


def _path(searched_path, relative_path):
    return FilePath(
        project_root="/users/root",
        searched_path=searched_path,
        relative_path=relative_path,
        modification_time=time.time(),
    )


def make_unit_test(name, model="model_a", version=None, given_fixtures=(), expect_fixture=None):
    given = [UnitTestInputFixture(input="ref('model_a')", fixture=f) for f in given_fixtures]
    expect = (
        UnitTestOutputFixture(fixture=expect_fixture)
        if expect_fixture
        else UnitTestOutputFixture(rows=[{"id": 1}])
    )
    unique_id = f"unit_test.{PROJECT}.{model}.{name}"
    if version is not None:
        unique_id += f"_v{version}"
    return UnitTestDefinition(
        name=name,
        model=model,
        resource_type=NodeType.Unit,
        package_name=PROJECT,
        path="unit_tests.yml",
        original_file_path="models/unit_tests.yml",
        unique_id=unique_id,
        given=given,
        expect=expect,
        depends_on=DependsOn(nodes=[f"model.{PROJECT}.{model}"]),
        fqn=[PROJECT, model, name],
        version=version,
    )


def make_schema_file(unit_test_dicts, unit_tests=None):
    return SchemaSourceFile(
        path=_path("models", "unit_tests.yml"),
        checksum=FileHash.from_contents("schema"),
        project_name=PROJECT,
        parse_file_type=ParseFileType.Schema,
        dfy={"version": 2, "unit_tests": unit_test_dicts},
        unit_tests=list(unit_tests or []),
    )


def make_fixture_file(name, unit_tests=None, checksum="fixture"):
    source_file = FixtureSourceFile(
        path=_path("tests/fixtures", f"{name}.csv"),
        checksum=FileHash.from_contents(checksum),
        project_name=PROJECT,
        parse_file_type=ParseFileType.Fixture,
    )
    source_file.fixture = f"fixture.{PROJECT}.{name}"
    source_file.unit_tests = list(unit_tests or [])
    return source_file


def make_manifest(files, unit_tests):
    manifest = Manifest(files={f.file_id: f for f in files})
    manifest.unit_tests = {ut.unique_id: ut for ut in unit_tests}
    for file in files:
        if isinstance(file, FixtureSourceFile):
            manifest.fixtures[file.fixture] = type(
                "FakeFixture", (), {"name": file.path.relative_path[:-4], "file_id": file.file_id}
            )()
    return manifest


@pytest.fixture
def safe_context():
    safe_set_invocation_context()


class TestFixtureNames:
    def test_includes_inputs_and_expect_without_duplicates(self):
        unit_test = make_unit_test(
            "echo",
            given_fixtures=("shared", "other"),
            expect_fixture="shared",
        )
        assert unit_test.fixture_names() == ["shared", "other"]

    def test_empty_when_all_rows_inline(self):
        assert make_unit_test("echo").fixture_names() == []


class TestDeleteFixtureNode:
    def _partial(self, schema_file, fixture_file, unit_tests):
        files = {schema_file.file_id: schema_file, fixture_file.file_id: fixture_file}
        manifest = make_manifest(list(files.values()), unit_tests)
        return PartialParsing(manifest, deepcopy(files))

    def test_rebuilds_version_expanded_definitions(self, safe_context):
        # The schema file only knows the pre-expansion id, as an older cache would.
        schema_file = make_schema_file(
            [{"name": "echo", "model": "model_a"}],
            unit_tests=[f"unit_test.{PROJECT}.model_a.echo"],
        )
        fixture_file = make_fixture_file(
            "shared", unit_tests=[f"unit_test.{PROJECT}.model_a.echo"]
        )
        unit_tests = [
            make_unit_test("echo", version=1, given_fixtures=("shared",)),
            make_unit_test("echo", version=2, given_fixtures=("shared",)),
        ]
        partial = self._partial(schema_file, fixture_file, unit_tests)

        partial.delete_fixture_node(fixture_file)

        # Both expanded definitions are gone and the schema file is scheduled.
        assert partial.saved_manifest.unit_tests == {}
        assert schema_file.file_id in partial.project_parser_files[PROJECT]["SchemaParser"]
        assert fixture_file.file_id not in partial.saved_manifest.files
        assert partial.saved_manifest.fixtures == {}
        # The yaml element is queued for reparsing.
        assert [elem["name"] for elem in schema_file.pp_dict["unit_tests"]] == ["echo"]

    def test_shared_fixture_rebuilds_every_referencing_test(self, safe_context):
        schema_file = make_schema_file(
            [
                {"name": "first", "model": "model_a"},
                {"name": "second", "model": "model_a"},
            ],
            unit_tests=[f"unit_test.{PROJECT}.model_a.first"],
        )
        fixture_file = make_fixture_file(
            "shared", unit_tests=[f"unit_test.{PROJECT}.model_a.first"]
        )
        unit_tests = [
            make_unit_test("first", given_fixtures=("shared",)),
            make_unit_test("second", given_fixtures=("shared",)),
        ]
        partial = self._partial(schema_file, fixture_file, unit_tests)

        partial.delete_fixture_node(fixture_file)

        # The second test was not listed on the fixture file, but it reads the same
        # fixture and must be rebuilt too.
        assert partial.saved_manifest.unit_tests == {}
        assert [elem["name"] for elem in schema_file.pp_dict["unit_tests"]] == [
            "first",
            "second",
        ]

    def test_test_using_fixture_twice_is_rebuilt_once(self, safe_context):
        schema_file = make_schema_file(
            [{"name": "echo", "model": "model_a"}],
            unit_tests=[f"unit_test.{PROJECT}.model_a.echo"],
        )
        fixture_file = make_fixture_file(
            "shared", unit_tests=[f"unit_test.{PROJECT}.model_a.echo"]
        )
        unit_tests = [make_unit_test("echo", given_fixtures=("shared",), expect_fixture="shared")]
        partial = self._partial(schema_file, fixture_file, unit_tests)

        partial.delete_fixture_node(fixture_file)

        assert partial.saved_manifest.unit_tests == {}
        # The yaml element is queued exactly once, not once per reference.
        assert [elem["name"] for elem in schema_file.pp_dict["unit_tests"]] == ["echo"]

    def test_stale_ids_are_harmless(self, safe_context):
        # Fixture file lists a test that no longer exists.
        schema_file = make_schema_file([{"name": "echo", "model": "model_a"}])
        fixture_file = make_fixture_file(
            "shared", unit_tests=[f"unit_test.{PROJECT}.model_a.deleted"]
        )
        unit_tests = [make_unit_test("echo", given_fixtures=("shared",))]
        partial = self._partial(schema_file, fixture_file, unit_tests)

        partial.delete_fixture_node(fixture_file)

        assert partial.saved_manifest.unit_tests == {}

    def test_unreferenced_test_is_not_rebuilt(self, safe_context):
        schema_file = make_schema_file(
            [
                {"name": "on_shared", "model": "model_a"},
                {"name": "on_other", "model": "model_a"},
            ]
        )
        fixture_file = make_fixture_file("shared")
        unit_tests = [
            make_unit_test("on_shared", given_fixtures=("shared",)),
            make_unit_test("on_other", given_fixtures=("other",)),
        ]
        partial = self._partial(schema_file, fixture_file, unit_tests)

        partial.delete_fixture_node(fixture_file)

        assert set(partial.saved_manifest.unit_tests) == {f"unit_test.{PROJECT}.model_a.on_other"}
        assert [elem["name"] for elem in schema_file.pp_dict["unit_tests"]] == ["on_shared"]


class TestRebuildIndexes:
    def _manifest_with_expanded_tests(self):
        schema_file = make_schema_file(
            [{"name": "echo", "model": "model_a"}],
            unit_tests=[f"unit_test.{PROJECT}.model_a.echo"],
        )
        fixture_file = make_fixture_file("shared")
        unit_tests = [
            make_unit_test("echo", version=1, given_fixtures=("shared",)),
            make_unit_test("echo", version=2, given_fixtures=("shared",)),
        ]
        return make_manifest([schema_file, fixture_file], unit_tests), schema_file, fixture_file

    def test_rebuild_fixture_usage_lists_expanded_definitions(self, safe_context):
        manifest, _, fixture_file = self._manifest_with_expanded_tests()
        manifest.rebuild_fixture_usage()
        assert fixture_file.unit_tests == [
            f"unit_test.{PROJECT}.model_a.echo_v1",
            f"unit_test.{PROJECT}.model_a.echo_v2",
        ]

    def test_rebuild_is_idempotent(self, safe_context):
        manifest, _, fixture_file = self._manifest_with_expanded_tests()
        manifest.rebuild_fixture_usage()
        manifest.rebuild_fixture_usage()
        manifest.rebuild_unit_test_source_links()
        manifest.rebuild_unit_test_source_links()
        assert fixture_file.unit_tests == [
            f"unit_test.{PROJECT}.model_a.echo_v1",
            f"unit_test.{PROJECT}.model_a.echo_v2",
        ]

    def test_rebuild_source_links_uses_existing_definitions(self, safe_context):
        manifest, schema_file, _ = self._manifest_with_expanded_tests()
        assert schema_file.unit_tests == [f"unit_test.{PROJECT}.model_a.echo"]
        manifest.rebuild_unit_test_source_links()
        assert schema_file.unit_tests == [
            f"unit_test.{PROJECT}.model_a.echo_v1",
            f"unit_test.{PROJECT}.model_a.echo_v2",
        ]
