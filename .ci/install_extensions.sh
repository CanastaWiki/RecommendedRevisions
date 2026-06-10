#!/usr/bin/env bash
# install_extensions.sh — Install extensions/skins into a MediaWiki instance
# based on the JSON manifest produced by parse_yaml.py.
#
# Usage:
#   install_extensions.sh <yaml_file> [batch]
#
# This script is designed to run INSIDE the MediaWiki Docker container.
# It expects the repo to be mounted at /ci/repo.

set -euo pipefail

YAML_FILE="${1:?Usage: install_extensions.sh <yaml_file> [batch]}"
BATCH="${2:-}"  # Optional: filter to a single batch.

REPO_DIR="/ci/repo"
MW_DIR="/var/www/html"
SCRIPTS_DIR="${REPO_DIR}/scripts"
CI_DIR="${REPO_DIR}/.ci"
RESULTS_DIR="/ci/test-results"
LOCALSETTINGS="${MW_DIR}/LocalSettings.php"

# Ensure results directory exists.
mkdir -p "${RESULTS_DIR}"

# ── Step 0: Generate the manifest ───────────────────────────────────────────

echo "==> Generating manifest from ${YAML_FILE}..."
MANIFEST_FILE="/tmp/manifest.json"
BATCH_ARGS=""
if [ -n "${BATCH}" ]; then
    BATCH_ARGS="--batch ${BATCH}"
fi

python3 "${SCRIPTS_DIR}/parse_yaml.py" \
    "${REPO_DIR}/${YAML_FILE}" \
    --ci-dir "${CI_DIR}" \
    ${BATCH_ARGS} \
    -o "${MANIFEST_FILE}"

TOTAL=$(python3 -c "import json; d=json.load(open('${MANIFEST_FILE}')); print(d['total_entries'])")
echo "    Manifest has ${TOTAL} entries."

# ── Step 1: Clone / checkout extensions and skins ────────────────────────────

echo "==> Installing extensions and skins..."

# Use python to iterate the manifest (more robust JSON handling than jq in
# the base mediawiki image which may not have jq).
python3 - "${MANIFEST_FILE}" "${MW_DIR}" <<'INSTALL_SCRIPT'
import json
import os
import subprocess
import sys

manifest_path = sys.argv[1]
mw_dir = sys.argv[2]

with open(manifest_path) as f:
    manifest = json.load(f)

entries = manifest["entries"]
installed = []
skipped_entries = []

for entry in entries:
    name = entry["name"]
    kind = entry["kind"]
    bundled = entry["bundled"]
    repo = entry["repository"]
    branch = entry["branch"]
    commit = entry.get("commit")
    skip = entry.get("skip", False)

    target_dir = os.path.join(
        mw_dir,
        "extensions" if kind == "extension" else "skins",
        name,
    )

    if skip:
        print(f"  SKIP  {kind:9s}  {name}  (reason: {entry.get('skip_reason', 'unknown')})")
        skipped_entries.append(name)
        continue

    if bundled:
        if os.path.isdir(target_dir):
            print(f"  OK    {kind:9s}  {name}  (bundled, already present)")
            installed.append(name)
        else:
            print(f"  WARN  {kind:9s}  {name}  (bundled but directory missing!)")
        continue

    # Clone the repository.
    if os.path.isdir(target_dir):
        print(f"  EXISTS {kind:8s}  {name}  (directory already present, updating)")
        subprocess.run(["git", "fetch", "--all"], cwd=target_dir, check=False,
                       capture_output=True)
    else:
        print(f"  CLONE {kind:8s}  {name}  <- {repo} (branch: {branch})")
        cmd = ["git", "clone"]
        if branch:
            cmd.extend(["--branch", branch, "--single-branch"])
        cmd.extend([repo, target_dir])
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  ERROR cloning {name}: {result.stderr.strip()}")
            skipped_entries.append(name)
            continue

    # Checkout specific commit if specified.
    if commit:
        result = subprocess.run(
            ["git", "checkout", commit],
            cwd=target_dir, capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"  ERROR checking out {commit} for {name}: {result.stderr.strip()}")
            skipped_entries.append(name)
            continue

    installed.append(name)

# ── Run additional steps (composer update, submodules) ───────────────────────

print("\n==> Running additional steps...")
for entry in entries:
    name = entry["name"]
    if name in skipped_entries:
        continue

    kind = entry["kind"]
    target_dir = os.path.join(
        mw_dir,
        "extensions" if kind == "extension" else "skins",
        name,
    )

    for step in entry.get("additional_steps", []):
        if step == "database update":
            # Database updates are batched later.
            continue
        elif step == "composer update":
            if os.path.isfile(os.path.join(target_dir, "composer.json")):
                print(f"  COMPOSER  {name}")
                subprocess.run(
                    ["composer", "update", "--no-interaction", "--no-progress",
                     "--prefer-dist", "--no-dev"],
                    cwd=target_dir, capture_output=True, check=False,
                )
        elif step == "git submodule update":
            print(f"  SUBMODULE {name}")
            subprocess.run(
                ["git", "submodule", "update", "--init", "--recursive"],
                cwd=target_dir, capture_output=True, check=False,
            )
        else:
            print(f"  UNKNOWN STEP for {name}: {step}")

# ── Write wfLoadExtension / wfLoadSkin lines to LocalSettings.php ────────────

print("\n==> Appending load lines to LocalSettings.php...")
ls_path = os.path.join(mw_dir, "LocalSettings.php")
load_lines = []
for entry in entries:
    name = entry["name"]
    if name in skipped_entries:
        continue

    kind = entry["kind"]
    target_dir = os.path.join(
        mw_dir,
        "extensions" if kind == "extension" else "skins",
        name,
    )
    # Only load extensions/skins that are actually present on disk.
    if not os.path.isdir(target_dir):
        continue

    if kind == "extension":
        if os.path.isfile(os.path.join(target_dir, "extension.json")):
            load_lines.append(f"wfLoadExtension( '{name}' );")
        elif os.path.isfile(os.path.join(target_dir, f"{name}.php")):
            load_lines.append(f"require_once \"$IP/extensions/{name}/{name}.php\";")
        else:
            print(f"  WARN: Could not find entry point for extension {name}")
    else:  # skin
        if os.path.isfile(os.path.join(target_dir, "skin.json")):
            load_lines.append(f"wfLoadSkin( '{name}' );")
        elif os.path.isfile(os.path.join(target_dir, f"{name}.php")):
            load_lines.append(f"require_once \"$IP/skins/{name}/{name}.php\";")
        else:
            print(f"  WARN: Could not find entry point for skin {name}")

with open(ls_path, "a") as f:
    f.write("\n".join(load_lines) + "\n")

print(f"  Added {len(load_lines)} load lines.")

# ── Write installed list for run_tests.sh ────────────────────────────────────
with open("/tmp/installed_entries.json", "w") as f:
    json.dump([e for e in entries if e["name"] not in skipped_entries], f, indent=2)

print(f"\n==> Installation complete: {len(installed)} installed, {len(skipped_entries)} skipped.")
INSTALL_SCRIPT

# ── Step 2: Run database update (once, after all extensions are loaded) ──────

echo "==> Running database update..."
cd "${MW_DIR}"

# Install MediaWiki if not already installed.
if [ ! -f "${LOCALSETTINGS}" ]; then
    echo "  Installing MediaWiki..."
    php maintenance/run.php install \
        --dbtype=mysql \
        --dbserver=db \
        --dbname=mediawiki \
        --dbuser=wiki \
        --dbpass=wiki_password \
        --pass=ci_admin_password \
        --scriptpath="" \
        "RecommendedRevisions CI" "Admin" \
        2>&1 || true
    # Restore our custom LocalSettings after install overwrites it.
    cp "${CI_DIR}/LocalSettings.ci.php" "${LOCALSETTINGS}"
    # Re-run the python install to re-append load lines.
    python3 - /tmp/installed_entries.json "${MW_DIR}" <<'REAPPEND'
import json, os, sys
entries = json.load(open(sys.argv[1]))
mw_dir = sys.argv[2]
ls_path = os.path.join(mw_dir, "LocalSettings.php")
lines = []
for e in entries:
    target_dir = os.path.join(mw_dir, "extensions" if e["kind"] == "extension" else "skins", e["name"])
    if not os.path.isdir(target_dir):
        continue
    fn = "wfLoadExtension" if e["kind"] == "extension" else "wfLoadSkin"
    lines.append(f'{fn}( \'{e["name"]}\' );')
with open(ls_path, "a") as f:
    f.write("\n".join(lines) + "\n")
REAPPEND
fi

# Run update.php to apply all schema changes.
php maintenance/run.php update --quick 2>&1 || {
    echo "  WARNING: update.php returned non-zero (some extensions may have issues)"
}

echo "==> MediaWiki is ready for testing."
