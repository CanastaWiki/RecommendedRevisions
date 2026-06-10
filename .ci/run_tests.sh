#!/usr/bin/env bash
# run_tests.sh — Run PHPUnit and parser tests for installed extensions/skins.
#
# Usage:
#   run_tests.sh [batch]
#
# Reads the installed entries from /tmp/installed_entries.json (written by
# install_extensions.sh) and runs tests for each one that has a test directory.
#
# Results are written as JUnit XML to /ci/test-results/.

set -uo pipefail  # Don't -e: we want to continue past individual test failures.

BATCH="${1:-}"
MW_DIR="/var/www/html"
RESULTS_DIR="/ci/test-results"
SUMMARY_FILE="${RESULTS_DIR}/summary.json"

mkdir -p "${RESULTS_DIR}"

# ── Discover testable entries ────────────────────────────────────────────────

echo "==> Discovering tests..."

python3 - "${BATCH}" <<'DISCOVER'
import json
import os
import sys

batch = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
mw_dir = "/var/www/html"
entries = json.load(open("/tmp/installed_entries.json"))

testable = []
no_tests = []

for entry in entries:
    if batch and entry.get("batch") != batch:
        continue

    name = entry["name"]
    kind = entry["kind"]
    base = os.path.join(mw_dir, "extensions" if kind == "extension" else "skins", name)
    test_dir = os.path.join(base, "tests", "phpunit")
    parser_test_dir = os.path.join(base, "tests", "parser")

    has_phpunit = os.path.isdir(test_dir)
    has_parser = os.path.isdir(parser_test_dir)

    if has_phpunit or has_parser:
        entry["has_phpunit"] = has_phpunit
        entry["has_parser"] = has_parser
        entry["test_dir"] = test_dir if has_phpunit else None
        entry["parser_test_dir"] = parser_test_dir if has_parser else None
        testable.append(entry)
    else:
        no_tests.append(name)

print(f"  {len(testable)} entries have tests, {len(no_tests)} have no tests.")
if no_tests:
    print(f"  No tests: {', '.join(no_tests[:20])}" + ("..." if len(no_tests) > 20 else ""))

with open("/tmp/testable_entries.json", "w") as f:
    json.dump(testable, f, indent=2)
with open("/tmp/no_tests.json", "w") as f:
    json.dump(no_tests, f, indent=2)
DISCOVER

# ── Run PHPUnit tests ────────────────────────────────────────────────────────

echo ""
echo "==> Running PHPUnit tests..."

PASSED=0
FAILED=0
ERRORED=0
SKIPPED_TEST=0
TOTAL=0

# Read the testable entries and run tests for each.
python3 -c "
import json
entries = json.load(open('/tmp/testable_entries.json'))
for e in entries:
    if e.get('has_phpunit'):
        print(f\"{e['name']}|{e['kind']}|{e['test_dir']}\")
" | while IFS='|' read -r NAME KIND TEST_DIR; do
    TOTAL=$((TOTAL + 1))
    RESULT_FILE="${RESULTS_DIR}/${NAME}.xml"

    echo ""
    echo "── Testing ${KIND}: ${NAME} ──────────────────────────────"

    # Run PHPUnit scoped to this extension's test directory.
    cd "${MW_DIR}"

    # Determine the phpunit runner.
    if [ -f "tests/phpunit/phpunit.php" ]; then
        RUNNER="php tests/phpunit/phpunit.php"
    elif [ -f "vendor/bin/phpunit" ]; then
        RUNNER="vendor/bin/phpunit"
    else
        echo "  WARNING: No PHPUnit runner found, skipping ${NAME}"
        SKIPPED_TEST=$((SKIPPED_TEST + 1))
        continue
    fi

    # Run with a timeout to prevent individual tests from hanging CI.
    timeout 300 ${RUNNER} \
        --log-junit "${RESULT_FILE}" \
        "${TEST_DIR}" \
        2>&1 | tail -20

    EXIT_CODE=${PIPESTATUS[0]}

    if [ ${EXIT_CODE} -eq 0 ]; then
        echo "  ✅ PASSED: ${NAME}"
        PASSED=$((PASSED + 1))
    elif [ ${EXIT_CODE} -eq 124 ]; then
        echo "  ⏱️  TIMEOUT: ${NAME} (exceeded 300s)"
        ERRORED=$((ERRORED + 1))
    else
        echo "  ❌ FAILED: ${NAME} (exit code ${EXIT_CODE})"
        FAILED=$((FAILED + 1))
    fi
done

# ── Run parser tests ─────────────────────────────────────────────────────────

echo ""
echo "==> Running parser tests..."

python3 -c "
import json
entries = json.load(open('/tmp/testable_entries.json'))
for e in entries:
    if e.get('has_parser'):
        print(f\"{e['name']}|{e['parser_test_dir']}\")
" | while IFS='|' read -r NAME PARSER_DIR; do
    echo ""
    echo "── Parser tests: ${NAME} ──────────────────────────────"

    cd "${MW_DIR}"

    # Find parser test files.
    PARSER_FILES=$(find "${PARSER_DIR}" -name "*.txt" -o -name "*.json" 2>/dev/null)
    if [ -z "${PARSER_FILES}" ]; then
        echo "  No parser test files found."
        continue
    fi

    # Run via PHPUnit parser test suite if available.
    if [ -f "tests/phpunit/phpunit.php" ]; then
        timeout 300 php tests/phpunit/phpunit.php \
            --log-junit "${RESULTS_DIR}/${NAME}-parser.xml" \
            --testsuite parsertests \
            2>&1 | tail -10 || true
        echo "  Parser tests completed for ${NAME}."
    fi
done

# ── Generate summary ─────────────────────────────────────────────────────────

echo ""
echo "==> Generating test summary..."

python3 - "${RESULTS_DIR}" <<'SUMMARY'
import json
import os
import sys
import xml.etree.ElementTree as ET

results_dir = sys.argv[1]
summary = {
    "extensions": {},
    "totals": {"passed": 0, "failed": 0, "errored": 0, "skipped": 0},
}

# Parse JUnit XML files.
for fname in sorted(os.listdir(results_dir)):
    if not fname.endswith(".xml"):
        continue
    ext_name = fname.replace(".xml", "").replace("-parser", " (parser)")
    fpath = os.path.join(results_dir, fname)
    try:
        tree = ET.parse(fpath)
        root = tree.getroot()

        tests = int(root.attrib.get("tests", 0))
        failures = int(root.attrib.get("failures", 0))
        errors = int(root.attrib.get("errors", 0))
        skipped = int(root.attrib.get("skipped", root.attrib.get("skip", 0)))

        status = "passed" if (failures == 0 and errors == 0) else "failed"
        summary["extensions"][ext_name] = {
            "tests": tests,
            "failures": failures,
            "errors": errors,
            "skipped": skipped,
            "status": status,
        }
        if status == "passed":
            summary["totals"]["passed"] += 1
        else:
            summary["totals"]["failed"] += 1
    except Exception as e:
        summary["extensions"][ext_name] = {
            "status": "error",
            "error": str(e),
        }
        summary["totals"]["errored"] += 1

# Add entries with no tests.
no_tests = json.load(open("/tmp/no_tests.json"))
for name in no_tests:
    summary["extensions"][name] = {"status": "no_tests"}

# Write summary.
summary_path = os.path.join(results_dir, "summary.json")
with open(summary_path, "w") as f:
    json.dump(summary, f, indent=2)

# Print human-readable summary.
print()
print("=" * 60)
print("TEST SUMMARY")
print("=" * 60)
print(f"  Passed:    {summary['totals']['passed']}")
print(f"  Failed:    {summary['totals']['failed']}")
print(f"  Errored:   {summary['totals']['errored']}")
print(f"  No tests:  {len(no_tests)}")
print()

if summary["totals"]["failed"] > 0:
    print("FAILED extensions:")
    for name, info in summary["extensions"].items():
        if info.get("status") == "failed":
            print(f"  ❌ {name}: {info.get('failures', 0)} failures, {info.get('errors', 0)} errors")
    print()

print(f"Full results in: {results_dir}/")
SUMMARY

# ── Generate GitHub Actions step summary (if running in Actions) ─────────────

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    echo "==> Writing GitHub Actions step summary..."
    python3 - "${RESULTS_DIR}" "${GITHUB_STEP_SUMMARY}" <<'GH_SUMMARY'
import json
import os
import sys

results_dir = sys.argv[1]
summary_file = sys.argv[2]
summary = json.load(open(os.path.join(results_dir, "summary.json")))

lines = [
    "## 🧪 Extension/Skin Test Results",
    "",
    f"| Metric | Count |",
    f"|--------|-------|",
    f"| ✅ Passed | {summary['totals']['passed']} |",
    f"| ❌ Failed | {summary['totals']['failed']} |",
    f"| ⚠️ Errored | {summary['totals']['errored']} |",
    "",
]

# Failed details.
failed = {k: v for k, v in summary["extensions"].items() if v.get("status") == "failed"}
if failed:
    lines.append("### ❌ Failed Extensions")
    lines.append("")
    lines.append("| Extension | Tests | Failures | Errors |")
    lines.append("|-----------|-------|----------|--------|")
    for name, info in sorted(failed.items()):
        lines.append(f"| {name} | {info.get('tests', '?')} | {info.get('failures', '?')} | {info.get('errors', '?')} |")
    lines.append("")

# Passed details (collapsed).
passed = {k: v for k, v in summary["extensions"].items() if v.get("status") == "passed"}
if passed:
    lines.append("<details><summary>✅ Passed Extensions ({} total)</summary>".format(len(passed)))
    lines.append("")
    lines.append("| Extension | Tests |")
    lines.append("|-----------|-------|")
    for name, info in sorted(passed.items()):
        lines.append(f"| {name} | {info.get('tests', '?')} |")
    lines.append("")
    lines.append("</details>")

with open(summary_file, "a") as f:
    f.write("\n".join(lines) + "\n")
GH_SUMMARY
fi

echo ""
echo "==> Test run complete."
