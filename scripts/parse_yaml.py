#!/usr/bin/env python3
from __future__ import annotations
"""
parse_yaml.py — Parse RecommendedRevisions YAML files and produce a JSON
manifest that CI scripts can consume to install and test extensions/skins.

Usage:
    python parse_yaml.py 1.43.yaml                      # Print JSON manifest
    python parse_yaml.py 1.43.yaml --validate            # Validate only
    python parse_yaml.py 1.43.yaml --validate-commits    # Verify pinned commits exist
    python parse_yaml.py 1.43.yaml --matrix              # Output batch matrix for GitHub Actions
    python parse_yaml.py 1.43.yaml --coexistence-matrix  # Output co-existence group matrix
    python parse_yaml.py 1.43.yaml --batch bundled       # Filter to one batch
    python parse_yaml.py 1.43.yaml --generate-localsettings  # Emit wfLoadExtension lines
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict

import yaml


# ── Constants ────────────────────────────────────────────────────────────────

GERRIT_EXT_BASE = "https://gerrit.wikimedia.org/r/mediawiki/extensions/{name}"
GERRIT_SKIN_BASE = "https://gerrit.wikimedia.org/r/mediawiki/skins/{name}"

# Batch names used for parallelisation in CI.
BATCH_BUNDLED = "bundled"
BATCH_SMW = "smw-ecosystem"
BATCH_STANDALONE_AL = "standalone-a-l"
BATCH_STANDALONE_MZ = "standalone-m-z"
BATCH_SKINS = "skins"

ALL_BATCHES = [BATCH_BUNDLED, BATCH_SMW, BATCH_STANDALONE_AL, BATCH_STANDALONE_MZ, BATCH_SKINS]

# Extensions that belong to the Semantic MediaWiki ecosystem.
SMW_EXTENSIONS = {
    "SemanticMediaWiki",
    "SemanticResultFormats",
    "SemanticCompoundQueries",
    "SemanticDependencyUpdater",
    "SemanticDrilldown",
    "SemanticExtraSpecialProperties",
    "SemanticScribunto",
    "SemanticWatchlist",
    "SemanticBreadcrumbLinks",
    "SemanticFormsSelect",
    "SemanticTasks",
    "Mermaid",  # lives under SemanticMediaWiki GitHub org
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def extract_mw_version(yaml_path: str) -> str:
    """Extract the MediaWiki version from the filename, e.g. '1.43' from '1.43.yaml'."""
    basename = os.path.basename(yaml_path)
    match = re.match(r"(\d+\.\d+)\.yaml$", basename)
    if not match:
        raise ValueError(f"Cannot determine MW version from filename: {basename}")
    return match.group(1)


def default_branch_for(entry: dict, mw_version: str) -> str | None:
    """Return the default branch for an extension/skin entry."""
    if "branch" in entry:
        return entry["branch"]
    if "repository" in entry:
        # External (GitHub) repos: clone the default branch dynamically.
        return None
    # Wikimedia Gerrit repos default to REL1_XX.
    major, minor = mw_version.split(".")
    return f"REL{major}_{minor}"


def repo_url_for(name: str, entry: dict, kind: str) -> str | None:
    """Return the git clone URL.  None for bundled entries."""
    if entry.get("bundled"):
        return None
    if "repository" in entry:
        return entry["repository"]
    base = GERRIT_EXT_BASE if kind == "extension" else GERRIT_SKIN_BASE
    return base.format(name=name)


def parse_entries(raw_list: list, kind: str, mw_version: str) -> list[dict]:
    """
    Parse the extensions or skins list from the YAML into a uniform list
    of dicts, each representing one installable component.
    """
    entries = []
    for item in raw_list:
        if isinstance(item, str):
            # Bare name with no attributes (shouldn't happen in current YAMLs, but be safe).
            name = item
            attrs = {}
        elif isinstance(item, dict):
            # Each item is a single-key dict: {Name: {attrs...}}
            name = list(item.keys())[0]
            attrs = item[name] if item[name] is not None else {}
        else:
            continue

        entry = {
            "name": name,
            "kind": kind,
            "bundled": bool(attrs.get("bundled", False)),
            "repository": repo_url_for(name, attrs, kind),
            "branch": default_branch_for(attrs, mw_version),
            "commit": attrs.get("commit"),
            "wikidata_id": attrs.get("Wikidata ID"),
            "additional_steps": attrs.get("additional steps", []),
            "required_extensions": attrs.get("required extensions", []),
            "persistent_directories": attrs.get("persistent directories",
                                                 attrs.get("persistent-directories", [])),
        }
        entries.append(entry)
    return entries


# ── Per-extension dependency resolution ──────────────────────────────────────

def get_extension_deps(name: str, all_entries: list[dict]) -> list[dict]:
    """
    Recursively resolve declared dependencies for a single extension.
    Returns the ordered list (dependencies first, then the extension itself).
    """
    name_map = {e["name"]: e for e in all_entries}
    resolved: list[dict] = []
    seen: set[str] = set()

    def resolve(n: str) -> None:
        if n in seen or n not in name_map:
            return
        seen.add(n)
        entry = name_map[n]
        for dep in entry.get("required_extensions", []):
            resolve(dep)
        resolved.append(entry)

    resolve(name)
    return resolved


# ── Dependency resolution (topological sort) ─────────────────────────────────
# Preserved for the --generate-localsettings convenience command and
# co-existence group ordering.

def topological_sort(entries: list[dict]) -> list[dict]:
    """
    Sort entries so that every entry appears after its required_extensions.
    Entries without dependencies keep their original relative order.
    """
    name_map = {e["name"]: e for e in entries}
    in_degree = defaultdict(int)
    dependents = defaultdict(list)  # dependency -> [entries that need it]

    for e in entries:
        in_degree[e["name"]]  # ensure key exists even if 0
        for dep in e["required_extensions"]:
            in_degree[e["name"]] += 1
            dependents[dep].append(e["name"])

    # Kahn's algorithm — use a list (preserving insertion order for ties).
    queue = [e["name"] for e in entries if in_degree[e["name"]] == 0]
    sorted_names: list[str] = []

    while queue:
        n = queue.pop(0)
        sorted_names.append(n)
        for dep_name in dependents[n]:
            in_degree[dep_name] -= 1
            if in_degree[dep_name] == 0:
                queue.append(dep_name)

    if len(sorted_names) != len(entries):
        missing = set(e["name"] for e in entries) - set(sorted_names)
        print(f"WARNING: Circular or unresolvable dependencies for: {missing}", file=sys.stderr)
        # Append remaining entries at the end so we don't lose them.
        for e in entries:
            if e["name"] not in sorted_names:
                sorted_names.append(e["name"])

    return [name_map[n] for n in sorted_names if n in name_map]


# ── Commit validation ────────────────────────────────────────────────────────

def validate_commits(entries: list[dict]) -> list[str]:
    """
    Verify each pinned commit exists in its repository via git ls-remote.
    Returns a list of failure messages (empty = all good).
    """
    failures = []
    for e in entries:
        if e["bundled"] or not e.get("commit"):
            continue
        repo = e.get("repository")
        if not repo:
            continue

        commit = e["commit"]
        name = e["name"]
        branch = e.get("branch")

        print(f"  Checking {name} ({commit[:12]})...", end=" ", flush=True)

        try:
            # First: verify the branch exists (if specified).
            if branch:
                result = subprocess.run(
                    ["git", "ls-remote", "--exit-code", repo, f"refs/heads/{branch}"],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode != 0:
                    msg = f"{name}: branch '{branch}' not found in {repo}"
                    print(f"FAIL (branch missing)")
                    failures.append(msg)
                    continue

            # Second: try to find the commit.  ls-remote can't look up
            # arbitrary SHAs directly, so we do a shallow clone check.
            # For Gerrit repos, we can use the Gerrit REST API or just
            # trust that if the branch exists, the commit is on it.
            # For GitHub repos, we can use the API.
            #
            # Pragmatic approach: clone --depth=1 --branch, then verify
            # the commit is an ancestor.  But that's slow for 170 repos.
            #
            # Fastest reliable approach: attempt a fetch of the specific SHA.
            # If the server supports it, this works in one round-trip.
            import tempfile
            with tempfile.TemporaryDirectory() as tmpdir:
                # Init a bare repo and try to fetch the exact commit.
                subprocess.run(
                    ["git", "init", "--bare", tmpdir],
                    capture_output=True, check=True,
                )
                fetch_result = subprocess.run(
                    ["git", "-C", tmpdir, "fetch", "--depth=1", repo, commit],
                    capture_output=True, text=True, timeout=60,
                )
                if fetch_result.returncode != 0:
                    msg = (
                        f"{name}: commit {commit[:12]} not found in "
                        f"{repo} (branch: {branch or 'default'})"
                    )
                    print(f"FAIL")
                    failures.append(msg)
                else:
                    print(f"OK")
        except subprocess.TimeoutExpired:
            msg = f"{name}: timeout verifying commit {commit[:12]} against {repo}"
            print(f"TIMEOUT")
            failures.append(msg)
        except Exception as exc:
            msg = f"{name}: error verifying commit: {exc}"
            print(f"ERROR ({exc})")
            failures.append(msg)

    return failures


# ── Batch assignment ─────────────────────────────────────────────────────────

def assign_batch(entry: dict) -> str:
    """Assign an entry to a CI batch."""
    if entry["kind"] == "skin":
        return BATCH_SKINS
    if entry["bundled"]:
        return BATCH_BUNDLED
    if entry["name"] in SMW_EXTENSIONS:
        return BATCH_SMW
    first_letter = entry["name"][0].upper()
    if first_letter <= "L":
        return BATCH_STANDALONE_AL
    return BATCH_STANDALONE_MZ


# ── Skip list loading ────────────────────────────────────────────────────────

def load_skip_list(ci_dir: str) -> dict:
    """
    Load the categorized skip list from .ci/skip_list.yaml.

    Returns a dict:
      {
        "external_services": {"ExtName", ...},
        "upstream_test_compat": {"ExtName", ...},
      }
    """
    skip_path = os.path.join(ci_dir, "skip_list.yaml")
    if not os.path.exists(skip_path):
        return {"external_services": set(), "upstream_test_compat": set()}

    with open(skip_path) as f:
        data = yaml.safe_load(f)

    if not data:
        return {"external_services": set(), "upstream_test_compat": set()}

    result = {
        "external_services": set(),
        "upstream_test_compat": set(),
    }

    # Handle the new categorized format.
    for item in data.get("external_services", []):
        result["external_services"].add(item["name"])
    for item in data.get("upstream_test_compat", []):
        result["upstream_test_compat"].add(item["name"])

    # Backward compat: also handle the old flat "skip:" format.
    for item in data.get("skip", []):
        result["external_services"].add(item["name"])

    return result


def load_skip_list_with_reasons(ci_dir: str) -> dict:
    """Load skip list with reasons attached (for reporting)."""
    skip_path = os.path.join(ci_dir, "skip_list.yaml")
    if not os.path.exists(skip_path):
        return {}

    with open(skip_path) as f:
        data = yaml.safe_load(f)

    if not data:
        return {}

    reasons = {}
    for item in data.get("external_services", []):
        reasons[item["name"]] = {"reason": item["reason"], "category": "external_services"}
    for item in data.get("upstream_test_compat", []):
        reasons[item["name"]] = {"reason": item["reason"], "category": "upstream_test_compat"}
    # Backward compat.
    for item in data.get("skip", []):
        reasons[item["name"]] = {"reason": item["reason"], "category": "external_services"}

    return reasons


# ── Co-existence groups ──────────────────────────────────────────────────────

def load_coexistence_groups(ci_dir: str) -> list[dict]:
    """Load co-existence group definitions from .ci/coexistence_groups.yaml."""
    groups_path = os.path.join(ci_dir, "coexistence_groups.yaml")
    if not os.path.exists(groups_path):
        return []

    with open(groups_path) as f:
        data = yaml.safe_load(f)

    return data.get("groups", []) if data else []


# ── LocalSettings generation ────────────────────────────────────────────────

def generate_localsettings_lines(entries: list[dict]) -> str:
    """Generate wfLoadExtension / wfLoadSkin lines for LocalSettings.php."""
    lines = ["# Auto-generated by parse_yaml.py", ""]
    for e in entries:
        fn = "wfLoadExtension" if e["kind"] == "extension" else "wfLoadSkin"
        lines.append(f'{fn}( \'{e["name"]}\' );')
    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────────

def build_manifest(yaml_path: str, skip_data: dict | None = None) -> dict:
    """Parse a YAML file and return the full manifest dict."""
    mw_version = extract_mw_version(yaml_path)

    with open(yaml_path) as f:
        data = yaml.safe_load(f)

    extensions = parse_entries(data.get("extensions", []), "extension", mw_version)
    skins = parse_entries(data.get("skins", []), "skin", mw_version)

    all_entries = extensions + skins

    # Mark skipped entries based on categorized skip list.
    if skip_data:
        external = skip_data.get("external_services", set())
        upstream = skip_data.get("upstream_test_compat", set())

        for e in all_entries:
            if e["name"] in external:
                e["skip"] = True
                e["skip_reason"] = "Requires external service (see skip_list.yaml)"
                e["skip_category"] = "external_services"
            elif e["name"] in upstream:
                e["skip"] = False  # NOT skipped — still validated
                e["skip_tests"] = True
                e["skip_reason"] = "Upstream test compat issue (non-blocking)"
                e["skip_category"] = "upstream_test_compat"

        # Transitive skipping: if a required extension is fully skipped
        # (external_services), skip dependents too.
        changed = True
        while changed:
            changed = False
            for e in all_entries:
                if e.get("skip"):
                    continue
                for dep in e.get("required_extensions", []):
                    dep_entry = next((x for x in all_entries if x["name"] == dep), None)
                    if dep_entry and dep_entry.get("skip"):
                        e["skip"] = True
                        e["skip_reason"] = f"Requires skipped extension: {dep}"
                        e["skip_category"] = "transitive"
                        changed = True
                        break

    # Resolve per-extension declared dependencies (for isolated testing).
    for e in all_entries:
        deps = get_extension_deps(e["name"], all_entries)
        e["declared_deps"] = [d["name"] for d in deps if d["name"] != e["name"]]

    # Assign batches.
    for e in all_entries:
        e["batch"] = assign_batch(e)

    manifest = {
        "mw_version": mw_version,
        "yaml_file": os.path.basename(yaml_path),
        "total_entries": len(all_entries),
        "bundled_count": sum(1 for e in all_entries if e["bundled"]),
        "skipped_count": sum(1 for e in all_entries if e.get("skip")),
        "entries": all_entries,
    }
    return manifest


def get_batch_entries_with_dependencies(all_entries: list[dict], target_batch: str) -> list[dict]:
    """
    Get all entries belonging to target_batch plus all their transitive dependencies,
    preserving topological order.
    """
    # 1. Start with entries that are directly in target_batch
    batch_entries = [e for e in all_entries if e["batch"] == target_batch]

    # 2. Build map of name -> entry
    name_map = {e["name"]: e for e in all_entries}

    # 3. Recursively find dependencies
    resolved_names = set()
    to_resolve = []
    for e in batch_entries:
        resolved_names.add(e["name"])
        to_resolve.append(e)

    while to_resolve:
        current = to_resolve.pop(0)
        for dep_name in current.get("required_extensions", []):
            if dep_name in name_map and dep_name not in resolved_names:
                resolved_names.add(dep_name)
                to_resolve.append(name_map[dep_name])

    # 4. Filter the original all_entries list to preserve topological order
    return [e for e in all_entries if e["name"] in resolved_names]


def main():
    parser = argparse.ArgumentParser(description="Parse RecommendedRevisions YAML files.")
    parser.add_argument("yaml_file", help="Path to the YAML file (e.g. 1.43.yaml)")
    parser.add_argument("--validate", action="store_true", help="Validate only, print summary")
    parser.add_argument("--validate-commits", action="store_true",
                        help="Verify that every pinned commit exists in its repository. "
                             "Exits non-zero if any commit is missing (hard failure).")
    parser.add_argument("--matrix", action="store_true",
                        help="Output JSON array of batch names for GitHub Actions matrix")
    parser.add_argument("--coexistence-matrix", action="store_true",
                        help="Output JSON array of co-existence group names")
    parser.add_argument("--batch", choices=ALL_BATCHES, help="Filter output to a single batch")
    parser.add_argument("--generate-localsettings", action="store_true",
                        help="Output wfLoadExtension/wfLoadSkin lines")
    parser.add_argument("--ci-dir", default=None,
                        help="Path to .ci/ directory (for skip_list.yaml). "
                             "Defaults to .ci/ relative to the YAML file.")
    parser.add_argument("--output", "-o", default=None, help="Write output to file instead of stdout")

    args = parser.parse_args()

    if not os.path.exists(args.yaml_file):
        print(f"ERROR: File not found: {args.yaml_file}", file=sys.stderr)
        sys.exit(1)

    ci_dir = args.ci_dir or os.path.join(os.path.dirname(args.yaml_file) or ".", ".ci")

    # ── --validate-commits ───────────────────────────────────────────────
    if args.validate_commits:
        print("Validating that recommended commits exist...\n")
        # Build manifest without skip list — we want to validate ALL commits.
        manifest = build_manifest(args.yaml_file)
        failures = validate_commits(manifest["entries"])

        if failures:
            print(f"\n❌ COMMIT VALIDATION FAILED — {len(failures)} bad recommendation(s):\n")
            for f in failures:
                print(f"  • {f}")
            print(
                "\nThese are data errors in the YAML file. The recommended commit "
                "does not exist in the specified repository/branch. Fix the YAML "
                "before merging."
            )
            sys.exit(1)
        else:
            print(f"\n✅ All {manifest['total_entries']} entries have valid commits.")
            sys.exit(0)

    skip_data = load_skip_list(ci_dir)
    manifest = build_manifest(args.yaml_file, skip_data)

    # ── --validate ───────────────────────────────────────────────────────
    if args.validate:
        print(f"YAML file:       {manifest['yaml_file']}")
        print(f"MW version:      {manifest['mw_version']}")
        print(f"Total entries:   {manifest['total_entries']}")
        print(f"Bundled:         {manifest['bundled_count']}")
        print(f"Skipped (external services):  {manifest['skipped_count']}")
        print()

        # Per-batch summary.
        from collections import Counter
        batch_counts = Counter(e["batch"] for e in manifest["entries"])
        print("Batches:")
        for b in ALL_BATCHES:
            print(f"  {b:20s}  {batch_counts.get(b, 0)}")

        # Skip category summary.
        skip_cats = Counter(
            e.get("skip_category", "none")
            for e in manifest["entries"]
            if e.get("skip") or e.get("skip_tests")
        )
        print("\nSkip list breakdown:")
        for cat, count in skip_cats.most_common():
            print(f"  {cat:25s}  {count}")

        # Check for missing dependencies.
        all_names = {e["name"] for e in manifest["entries"]}
        missing_deps = []
        for e in manifest["entries"]:
            for dep in e["required_extensions"]:
                if dep not in all_names:
                    missing_deps.append((e["name"], dep))
        if missing_deps:
            print("\nWARNING: Missing required extensions:")
            for ext, dep in missing_deps:
                print(f"  {ext} requires {dep} (not in YAML)")
        else:
            print("\nAll required extension dependencies are satisfied.")

        sys.exit(0)

    # ── --matrix ─────────────────────────────────────────────────────────
    if args.matrix:
        # Output the list of batches that actually have entries.
        from collections import Counter
        batch_counts = Counter(e["batch"] for e in manifest["entries"])
        active = [b for b in ALL_BATCHES if batch_counts.get(b, 0) > 0]
        print(json.dumps(active))
        sys.exit(0)

    # ── --coexistence-matrix ─────────────────────────────────────────────
    if args.coexistence_matrix:
        groups = load_coexistence_groups(ci_dir)
        group_names = [g["name"] for g in groups]
        print(json.dumps(group_names))
        sys.exit(0)

    # ── --generate-localsettings ─────────────────────────────────────────
    if args.generate_localsettings:
        entries = manifest["entries"]
        if args.batch:
            entries = get_batch_entries_with_dependencies(entries, args.batch)
        entries = [e for e in entries if not e.get("skip")]
        # Sort for load ordering when generating all-at-once config.
        entries = topological_sort(entries)
        output = generate_localsettings_lines(entries)
        if args.output:
            with open(args.output, "w") as f:
                f.write(output + "\n")
            print(f"Wrote {len(entries)} load lines to {args.output}", file=sys.stderr)
        else:
            print(output)
        sys.exit(0)

    # ── Default: full JSON manifest ──────────────────────────────────────
    entries = manifest["entries"]
    if args.batch:
        manifest["entries"] = get_batch_entries_with_dependencies(entries, args.batch)
        manifest["total_entries"] = len(manifest["entries"])

    output = json.dumps(manifest, indent=2)
    if args.output:
        with open(args.output, "w") as f:
            f.write(output + "\n")
        print(f"Wrote manifest to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
