#!/usr/bin/env bash
# Reproduce and verify the unit-test fixture partial-parsing fix end to end.
#
# Sets up a fresh Python 3.12 virtualenv, installs dbt-core from this checkout
# plus dbt-duckdb, then:
#
#   1. builds the fixture_audit project from the report and shows the original
#      KeyError / "Switching to full reparse" failure is gone,
#   2. checks the warm parse against a full parse definition by definition,
#   3. runs the unit tests and observes the pass/fail changes as fixtures move,
#   4. runs the new regression suite plus the existing unit-tests/contracts
#      checks.
#
# Usage:
#   test/verify_unit_test_fixture_pp.sh [workdir]
#
# Everything it creates lives under <workdir> (default: a temp dir), so the
# repository is not touched.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKDIR="${1:-$(mktemp -d -t fixture-pp-XXXXXX)}"
VENV="${WORKDIR}/venv"
PROJECT="${WORKDIR}/fixture_audit"
PY="${VENV}/bin/python"
DBT="${VENV}/bin/dbt"

info() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

info "Working directory: ${WORKDIR}"

# ---------------------------------------------------------------------------
# 0. Python 3.12 environment
# ---------------------------------------------------------------------------
if [ ! -x "${PY}" ]; then
    if command -v uv >/dev/null 2>&1; then
        uv venv --python 3.12 "${VENV}"
        uv pip install --python "${PY}" -e "${REPO_ROOT}/core" dbt-duckdb pytest
    else
        python3.12 -m venv "${VENV}"
        "${PY}" -m pip install -e "${REPO_ROOT}/core" dbt-duckdb pytest
    fi
fi
"${PY}" --version
"${DBT}" --version | sed -n '1,3p'

# ---------------------------------------------------------------------------
# 1. fixture_audit project from the report
# ---------------------------------------------------------------------------
rm -rf "${PROJECT}"
mkdir -p "${PROJECT}/models" "${PROJECT}/tests/fixtures"
cat >"${PROJECT}/dbt_project.yml" <<'YAML'
name: fixture_audit
version: '1.0'
config-version: 2
profile: fixture_audit
YAML
cat >"${PROJECT}/profiles.yml" <<YAML
fixture_audit:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: ${WORKDIR}/fixture_audit.duckdb
YAML
printf 'select 1 as id\n' >"${PROJECT}/models/base.sql"
printf "select * from {{ ref('base') }}\n" >"${PROJECT}/models/sales_v1.sql"
printf "select * from {{ ref('base') }}\n" >"${PROJECT}/models/sales_v2.sql"
cat >"${PROJECT}/models/sales.yml" <<'YAML'
version: 2
models:
  - name: sales
    latest_version: 2
    versions:
      - v: 1
        defined_in: sales_v1
      - v: 2
        defined_in: sales_v2
YAML
cat >"${PROJECT}/tests/unit_sales.yml" <<'YAML'
version: 2
unit_tests:
  - name: echo
    model: sales
    given:
      - input: ref('base')
        format: csv
        fixture: shared
    expect:
      format: csv
      rows: |
        id
        1
YAML
printf 'id\n1\n' >"${PROJECT}/tests/fixtures/shared.csv"

cd "${PROJECT}"

run_parse_debug() { "${DBT}" parse --profiles-dir . --debug 2>&1; }
assert_no_fallback() {
    local log="$1"
    if grep -q "Switching to full reparse" <<<"${log}" ||
        grep -q "Unable to do partial parsing" <<<"${log}"; then
        echo "FAIL: partial parsing fell back to a full reparse" >&2
        grep -E "Switching|exception|KeyError" <<<"${log}" >&2 || true
        exit 1
    fi
}

# Projection helper: unit test definitions + model column/contract info.
read -r -d '' PROJECT_SCRIPT <<'PY' || true
import json, sys

def project(path):
    m = json.load(open(path))
    uts = {
        k: {
            "version": v.get("version"),
            "depends_on": v["depends_on"]["nodes"],
            "given": v["given"],
            "expect": v["expect"],
            "versions": v.get("versions"),
        }
        for k, v in m.get("unit_tests", {}).items()
    }
    models = {
        k: {
            "columns": sorted((v.get("columns") or {}).keys()),
            "contract": v["config"].get("contract"),
        }
        for k, v in m.get("nodes", {}).items()
        if v["resource_type"] == "model"
    }
    return {"unit_tests": uts, "models": models}

a, b = project(sys.argv[1]), project(sys.argv[2])
if a != b:
    print("DIFFERENT")
    print("warm:", json.dumps(a, indent=2, sort_keys=True))
    print("full:", json.dumps(b, indent=2, sort_keys=True))
    sys.exit(1)
print("IDENTICAL")
PY

info "1. Full parse, then edit the CSV and warm-parse"
"${DBT}" parse --profiles-dir . >/dev/null
cp target/manifest.json "${WORKDIR}/full_before.json"
printf 'id\n2\n' >tests/fixtures/shared.csv
LOG="$(run_parse_debug)"
assert_no_fallback "${LOG}"
grep -q "Partial parsing: updated file" <<<"${LOG}" || {
    echo "FAIL: fixture edit was not processed by partial parsing" >&2
    exit 1
}
echo "OK: no KeyError, no full reparse"

info "2. Compare warm parse with a full parse of the same files"
cp target/manifest.json "${WORKDIR}/warm.json"
rm -rf target logs
"${DBT}" parse --profiles-dir . >/dev/null
cp target/manifest.json "${WORKDIR}/full.json"
"${PY}" -c "${PROJECT_SCRIPT}" "${WORKDIR}/warm.json" "${WORKDIR}/full.json"

info "3. Run unit tests: input fixture drives pass/fail"
printf 'id\n1\n' >tests/fixtures/shared.csv
"${DBT}" build --profiles-dir . >/dev/null
echo "input = 1 (matches expect):"
"${DBT}" test --profiles-dir . 2>&1 | grep -E "Done\."
printf 'id\n3\n' >tests/fixtures/shared.csv
echo "input = 3 (does not match expect):"
"${DBT}" test --profiles-dir . 2>&1 | grep -E "Done\." || true
printf 'id\n1\n' >tests/fixtures/shared.csv
echo "input = 1 again:"
"${DBT}" test --profiles-dir . 2>&1 | grep -E "Done\."

info "4. Regression suites"
cd "${REPO_ROOT}"
# Unit-level bookkeeping plus the end-to-end DuckDB module, which also covers
# version include/exclude, cross-process cache reuse, rename/delete, reference
# swaps and version-level column contract selection.
"${PY}" -m pytest -q tests/unit
"${PY}" -m pytest -q tests/functional/unit_testing/test_ut_fixture_partial_parsing.py

info "All checks passed"
