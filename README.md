# Recommended Revisions

[![Extension/Skin Tests](https://github.com/CanastaWiki/RecommendedRevisions/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/CanastaWiki/RecommendedRevisions/actions/workflows/ci.yml)

Recommended Revisions is a project to try to find a consensus-based listing of the ideal revision to use for any extension or skin, for different MediaWiki versions. This repository holds YAML files containing the actual data for this project.

You can see more information at the project's homepage, [mediawiki.org/wiki/Recommended_Revisions](https://www.mediawiki.org/wiki/Recommended_Revisions).

---

## Continuous Integration (CI) Pipeline

The CI pipeline validates that every recommended revision is sound — that the commit resolves, dependencies install, the extension loads cleanly, and its tests pass. It separates *recommendation validity* from *upstream test-suite compat* so that flagship extensions like PageForms and Cargo always produce validation signal.

### Architecture Overview

The pipeline runs four jobs:

1. **Parse & Validate Commits:** A pre-flight check that verifies every pinned commit exists in its repository. A missing commit (e.g. a SHA that doesn't exist on the REL branch) is a **hard failure** — this is the highest-value thing the pipeline catches because it's a wrong recommendation.

2. **Per-Extension Isolated Validation:** Each extension is tested with *only its declared dependencies* loaded — not all 170 extensions in one config. For each extension:
   * Checkout the recommended commit.
   * Run `composer update` if needed.
   * Generate a minimal `LocalSettings.php` with only the extension and its declared deps.
   * Run the extension's PHPUnit test suite.

   This avoids conflating "is this revision safe to bundle?" with "does one everything-enabled wiki pass every integration suite?" Extensions requiring external services (Elasticsearch, LDAP, etc.) are skipped but reported; extensions with upstream test-suite compat issues are load-validated but their test failures are non-blocking.

3. **Co-existence Group Tests (non-blocking):** Curated groups of extensions that are commonly deployed together (e.g. the Semantic MediaWiki ecosystem, form/data extensions) are loaded into one MediaWiki instance to confirm they coexist cleanly. This provides intentional integration signal without the fragility of an everything-enabled config.

4. **Report:** Aggregates results with distinct sections for validation failures (blocking), upstream compat issues (non-blocking), co-existence results, and external-service skips.

### Running Tests Locally

#### 1. Validate the YAML manifest
```bash
python3 scripts/parse_yaml.py 1.43.yaml --validate
```

#### 2. Validate that recommended commits exist
```bash
python3 scripts/parse_yaml.py 1.43.yaml --validate-commits
```

#### 3. Run a specific test batch using Docker Compose
```bash
# Start the fast, RAM-backed MediaWiki container stack
MW_VERSION=1.43 docker compose -f .ci/docker-compose.ci.yml up -d

# Install the extension batch (e.g., smw-ecosystem)
docker compose -f .ci/docker-compose.ci.yml exec mediawiki bash /ci/install_extensions.sh 1.43.yaml smw-ecosystem

# Execute isolated per-extension tests for the batch
docker compose -f .ci/docker-compose.ci.yml exec mediawiki \
    python3 /ci/run_isolated_tests.py /tmp/manifest.json /var/www/html /var/www/html/LocalSettings.base.php smw-ecosystem /ci/test-results

# Spin down the stack
docker compose -f .ci/docker-compose.ci.yml down
```

#### 4. Skip List Configuration

The skip list (`.ci/skip_list.yaml`) is organized into two categories:

- **`external_services`** — Extensions requiring infrastructure not available in CI (Elasticsearch, LDAP, AWS, etc.). These skip all validation phases. This bucket should shrink over time as services are wired up.
- **`upstream_test_compat`** — Extensions that load correctly but have test suite issues with MW 1.43 / PHP 8.3. These are still validated (checkout, composer, load) but test failures are non-blocking.

#### 5. Co-existence Groups

Curated groups of extensions tested together are defined in `.ci/coexistence_groups.yaml`. Add groups for common co-deployment scenarios to catch interaction bugs without the everything-enabled fragility.

---

## Weekly Pin Updates

The `Pin updates` workflow runs every Monday (and on demand from the Actions tab). For each YAML file it scans, it clones every pinned repository and looks for significant commits between the pinned commit and the tracked branch. Translation updates, test and CI changes, linting fixes and other housekeeping commits are ignored. Results go into one issue per YAML file, labeled `pin-updates`, which the next scan refreshes.

The offered revision depends on the branch:

- **`REL1_xx` branches** — the branch head, ticked by default.
- **Other branches with newer release tags** — the newest stable tag, ticked by default unless its major version is higher than the pinned one.
- **Other branches without newer tags** — the branch head, ticked by default unless its declared major version is higher.

A revision is offered only when its `extension.json` or `skin.json` accepts the latest MediaWiki release for the file's branch. When the branch head requires a newer MediaWiki, the newest earlier revision that does not is offered instead.

To apply updates, tick the entries to take and add the `apply-pin-updates` label to the issue. The workflow opens a pull request that moves those pins and updates their `# v.` comments. Ticks survive the weekly refresh as long as the offered revision does not change.

Pull requests opened with the default `GITHUB_TOKEN` do not trigger the CI workflow. Set a `PIN_UPDATES_TOKEN` repository secret (a fine-grained token with contents, issues and pull-request write access) to have CI run on them; otherwise close and reopen the pull request to start CI.

To preview the issue locally:
```bash
python3 scripts/pin_updates.py scan 1.43.yaml --output body.md
```
