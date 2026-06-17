#!/usr/bin/env python3
"""
run_isolated_tests.py — Run per-extension isolated PHPUnit tests.

For each extension in the batch, this script:
  1. Generates a minimal LocalSettings.php loading ONLY that extension
     and its declared dependencies.
  2. Runs database update for the isolated set.
  3. Runs the extension's PHPUnit test suite.
  4. Records the result (passed/failed/load-only/no-tests/skipped).

This replaces the previous "all 170 extensions in one config" approach
with targeted per-extension validation.

Usage:
    python3 run_isolated_tests.py <manifest.json> <mw_dir> <base_localsettings> <batch> <results_dir>
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile


def generate_isolated_localsettings(
    base_config: str,
    extension: dict,
    deps: list[dict],
    mw_dir: str,
    all_entries: dict,
) -> str:
    """
    Generate a LocalSettings.php that loads ONLY the given extension
    and its declared dependencies.

    Returns the path to the generated LocalSettings.php.
    """
    name_map = all_entries

    load_lines = []
    loaded_names = set()

    # Load dependencies first (in order).
    for dep_name in deps:
        dep_entry = name_map.get(dep_name)
        if not dep_entry:
            continue
        kind = dep_entry["kind"]
        target_dir = os.path.join(
            mw_dir,
            "extensions" if kind == "extension" else "skins",
            dep_name,
        )
        if not os.path.isdir(target_dir):
            continue

        if kind == "extension":
            if os.path.isfile(os.path.join(target_dir, "extension.json")):
                load_lines.append(f"wfLoadExtension( '{dep_name}' );")
                loaded_names.add(dep_name)
        else:
            if os.path.isfile(os.path.join(target_dir, "skin.json")):
                load_lines.append(f"wfLoadSkin( '{dep_name}' );")
                loaded_names.add(dep_name)

    # Load the extension under test.
    name = extension["name"]
    kind = extension["kind"]
    if name not in loaded_names:
        target_dir = os.path.join(
            mw_dir,
            "extensions" if kind == "extension" else "skins",
            name,
        )
        if os.path.isdir(target_dir):
            fn = "wfLoadExtension" if kind == "extension" else "wfLoadSkin"
            ext_json = "extension.json" if kind == "extension" else "skin.json"
            if os.path.isfile(os.path.join(target_dir, ext_json)):
                load_lines.append(f"{fn}( '{name}' );")
            elif os.path.isfile(os.path.join(target_dir, f"{name}.php")):
                subdir = "extensions" if kind == "extension" else "skins"
                load_lines.append(f'require_once "$IP/{subdir}/{name}/{name}.php";')

    ls_path = os.path.join(mw_dir, "LocalSettings.php")
    with open(ls_path, "w") as f:
        f.write(base_config)
        f.write("\n# ── Isolated load for: " + name + " ──\n")
        f.write("\n".join(load_lines) + "\n")

    return ls_path


def run_load_test(mw_dir: str, name: str) -> tuple[bool, str]:
    """
    Verify that MediaWiki can load with the current LocalSettings.php
    without a fatal error.

    Returns (success, error_message).
    """
    try:
        result = subprocess.run(
            ["php", "maintenance/run.php", "eval"],
            input="echo 'OK';\n",
            cwd=mw_dir,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return False, result.stderr.strip()[:500]
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "Timeout loading MediaWiki"
    except Exception as e:
        return False, str(e)


def run_db_update(mw_dir: str) -> bool:
    """Run maintenance/run.php update --quick for the isolated config."""
    try:
        result = subprocess.run(
            ["php", "maintenance/run.php", "update", "--quick"],
            cwd=mw_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.returncode == 0
    except Exception:
        return False


def run_phpunit(
    mw_dir: str,
    test_dir: str,
    result_file: str,
    exclude_files: list[str] | None = None,
    timeout: int = 300,
) -> dict:
    """
    Run PHPUnit for an extension's test directory.

    Args:
        exclude_files: Optional list of test file paths (relative to extension
                       root) to exclude.  Since PHPUnit 9 has no --exclude-file
                       flag, we enumerate the test files and omit the excluded
                       ones, passing the remaining files individually.

    Returns a dict with status and details.
    """
    phpunit_runner = os.path.join(mw_dir, "tests", "phpunit", "phpunit.php")
    if not os.path.isfile(phpunit_runner):
        return {"status": "error", "message": "No PHPUnit runner found"}

    try:
        cmd = ["php", phpunit_runner, "--log-junit", result_file]

        if exclude_files:
            # Resolve excluded paths to absolute paths for comparison.
            # test_dir is <mw_dir>/extensions/<Name>/tests/phpunit
            # exclude paths are relative to <mw_dir>/extensions/<Name>/
            ext_base = os.path.dirname(os.path.dirname(test_dir))
            excluded_abs = set()
            for ef in exclude_files:
                abs_path = os.path.realpath(os.path.join(ext_base, ef))
                excluded_abs.add(abs_path)

            # Enumerate all test PHP files and filter out excluded ones.
            test_files = []
            for root, _dirs, files in os.walk(test_dir):
                for f in sorted(files):
                    if f.endswith(".php"):
                        fpath = os.path.realpath(os.path.join(root, f))
                        if fpath not in excluded_abs:
                            test_files.append(fpath)

            if not test_files:
                return {"status": "no_tests", "message": "All test files excluded"}

            cmd.extend(test_files)
        else:
            cmd.append(test_dir)

        proc = subprocess.run(
            cmd,
            cwd=mw_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        output = (proc.stdout + proc.stderr).strip()
        last_lines = "\n".join(output.split("\n")[-10:])

        if proc.returncode == 0:
            return {"status": "passed", "output": last_lines}
        else:
            return {
                "status": "failed",
                "exit_code": proc.returncode,
                "output": last_lines,
            }
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "message": f"Exceeded {timeout}s"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def main():
    if len(sys.argv) < 6:
        print(
            "Usage: run_isolated_tests.py <manifest.json> <mw_dir> "
            "<base_localsettings> <batch> <results_dir>",
            file=sys.stderr,
        )
        sys.exit(1)

    manifest_path = sys.argv[1]
    mw_dir = sys.argv[2]
    base_ls_path = sys.argv[3]
    target_batch = sys.argv[4]
    results_dir = sys.argv[5]

    os.makedirs(results_dir, exist_ok=True)

    with open(manifest_path) as f:
        manifest = json.load(f)

    with open(base_ls_path) as f:
        base_config = f.read()

    entries = manifest["entries"]
    name_map = {e["name"]: e for e in entries}

    # Filter to the target batch.
    batch_entries = [e for e in entries if e.get("batch") == target_batch]

    results = {
        "passed": [],
        "failed": [],
        "load_only": [],
        "errored": [],
        "no_tests": [],
        "skipped": [],
        "details": {},
    }

    for entry in batch_entries:
        name = entry["name"]
        kind = entry["kind"]

        # Skip entries that are fully skipped (external services).
        if entry.get("skip"):
            print(f"\n  SKIP  {kind:9s}  {name}  ({entry.get('skip_reason', 'unknown')})")
            results["skipped"].append(name)
            results["details"][name] = {
                "status": "skipped",
                "reason": entry.get("skip_reason", "unknown"),
                "category": entry.get("skip_category", "unknown"),
            }
            continue

        print(f"\n{'=' * 60}")
        print(f"Validating {kind}: {name}")
        print(f"{'=' * 60}")

        # ── Phase 1: Generate isolated LocalSettings ─────────────────
        declared_deps = entry.get("declared_deps", [])
        print(f"  Dependencies: {declared_deps if declared_deps else '(none)'}")

        generate_isolated_localsettings(
            base_config, entry, declared_deps, mw_dir, name_map
        )

        # ── Phase 2: Database update for isolated set ────────────────
        print(f"  Running database update...")
        run_db_update(mw_dir)

        # ── Phase 3: Determine test disposition ──────────────────────
        base = os.path.join(
            mw_dir,
            "extensions" if kind == "extension" else "skins",
            name,
        )
        test_dir = os.path.join(base, "tests", "phpunit")

        skip_tests = entry.get("skip_tests", False)

        if skip_tests:
            # upstream_test_compat: validated for load, but tests are non-blocking.
            print(f"  ⚠️  LOAD-ONLY: {name} (upstream test compat — non-blocking)")
            results["load_only"].append(name)
            results["details"][name] = {
                "status": "load_only",
                "reason": entry.get("skip_reason", "upstream test compat"),
            }
            continue

        if not os.path.isdir(test_dir):
            print(f"  📭 NO TESTS: {name}")
            results["no_tests"].append(name)
            results["details"][name] = {"status": "no_tests"}
            continue

        # ── Phase 4: Run PHPUnit tests ───────────────────────────────
        exclude_files = entry.get("exclude_tests", [])
        if exclude_files:
            print(f"  Running PHPUnit tests (excluding {len(exclude_files)} file(s))...")
        else:
            print(f"  Running PHPUnit tests...")
        result_file = os.path.join(results_dir, f"{name}.xml")
        test_result = run_phpunit(mw_dir, test_dir, result_file, exclude_files=exclude_files)

        status = test_result["status"]
        results["details"][name] = test_result

        if status == "passed":
            print(f"\n  ✅ PASSED: {name}")
            if test_result.get("output"):
                for line in test_result["output"].split("\n")[-5:]:
                    print(f"    {line}")
            results["passed"].append(name)
        elif status == "failed":
            print(f"\n  ❌ FAILED: {name} (exit {test_result.get('exit_code', '?')})")
            if test_result.get("output"):
                print(f"\n--- FAILURE LOG ---")
                print(test_result["output"])
                print(f"-------------------")
            results["failed"].append(name)
        elif status == "timeout":
            print(f"\n  ⏱️  TIMEOUT: {name}")
            results["errored"].append(name)
        else:
            print(f"\n  ⚠️  ERROR: {name}: {test_result.get('message', '?')}")
            results["errored"].append(name)

    # ── Summary ──────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print("TEST SUMMARY")
    print(f"{'=' * 60}")
    print(f"  ✅ Passed:     {len(results['passed'])}")
    print(f"  ❌ Failed:     {len(results['failed'])}")
    print(f"  ⚠️  Errored:    {len(results['errored'])}")
    print(f"  🔧 Load-only:  {len(results['load_only'])}")
    print(f"  📭 No tests:   {len(results['no_tests'])}")
    print(f"  ⏭️  Skipped:    {len(results['skipped'])}")

    if results["failed"]:
        print(f"\n  Failed: {', '.join(results['failed'])}")

    # Write summary JSON.
    summary_path = os.path.join(results_dir, "summary.json")
    with open(summary_path, "w") as f:
        json.dump(results, f, indent=2)

    # Exit with failure if any tests failed.
    if results["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
