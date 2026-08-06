"""Unit tests for scripts/parse_yaml.py.

These cover the pure logic that drives the CI pipeline — version extraction,
branch/URL derivation, entry parsing, dependency resolution, batch assignment,
skip-list categorization, and the transitive-skip loop in build_manifest —
without needing a MediaWiki install or network access.
"""

import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import parse_yaml  # noqa: E402


# ── extract_mw_version ───────────────────────────────────────────────────────

def test_extract_mw_version_from_basename():
    assert parse_yaml.extract_mw_version("1.43.yaml") == "1.43"
    assert parse_yaml.extract_mw_version("/some/path/1.44.yaml") == "1.44"


def test_extract_mw_version_rejects_bad_name():
    with pytest.raises(ValueError):
        parse_yaml.extract_mw_version("recommended.yaml")


# ── default_branch_for ───────────────────────────────────────────────────────

def test_default_branch_explicit_wins():
    assert parse_yaml.default_branch_for({"branch": "master"}, "1.43") == "master"


def test_default_branch_github_repo_is_none():
    # External repos clone their default branch dynamically.
    assert parse_yaml.default_branch_for({"repository": "https://x/y"}, "1.43") is None


def test_default_branch_gerrit_uses_rel_branch():
    assert parse_yaml.default_branch_for({}, "1.43") == "REL1_43"
    assert parse_yaml.default_branch_for({}, "1.44") == "REL1_44"


# ── repo_url_for ─────────────────────────────────────────────────────────────

def test_repo_url_bundled_is_none():
    assert parse_yaml.repo_url_for("Cite", {"bundled": True}, "extension") is None


def test_repo_url_explicit_repository():
    url = "https://github.com/ProfessionalWiki/Maps"
    assert parse_yaml.repo_url_for("Maps", {"repository": url}, "extension") == url


def test_repo_url_gerrit_default_by_kind():
    assert parse_yaml.repo_url_for("Cargo", {}, "extension") == (
        "https://gerrit.wikimedia.org/r/mediawiki/extensions/Cargo"
    )
    assert parse_yaml.repo_url_for("Modern", {}, "skin") == (
        "https://gerrit.wikimedia.org/r/mediawiki/skins/Modern"
    )


# ── parse_entries ────────────────────────────────────────────────────────────

def test_parse_entries_dict_item():
    raw = [{"AdminLinks": {"branch": "master", "commit": "abc",
                           "required extensions": ["Foo"]}}]
    entries = parse_yaml.parse_entries(raw, "extension", "1.43")
    assert len(entries) == 1
    e = entries[0]
    assert e["name"] == "AdminLinks"
    assert e["kind"] == "extension"
    assert e["branch"] == "master"
    assert e["commit"] == "abc"
    assert e["required_extensions"] == ["Foo"]
    assert e["bundled"] is False


def test_parse_entries_bundled_has_no_repo_and_rel_branch():
    raw = [{"Cite": {"bundled": True}}]
    (e,) = parse_yaml.parse_entries(raw, "extension", "1.43")
    assert e["bundled"] is True
    assert e["repository"] is None
    # No explicit branch/repository -> Gerrit REL branch.
    assert e["branch"] == "REL1_43"


def test_parse_entries_persistent_directories_both_spellings():
    spaced = parse_yaml.parse_entries(
        [{"Widgets": {"persistent directories": ["compiled_templates"]}}],
        "extension", "1.43",
    )[0]
    hyphen = parse_yaml.parse_entries(
        [{"Old": {"persistent-directories": ["compiled_templates"]}}],
        "extension", "1.43",
    )[0]
    assert spaced["persistent_directories"] == ["compiled_templates"]
    assert hyphen["persistent_directories"] == ["compiled_templates"]


def test_parse_entries_bare_string_item():
    (e,) = parse_yaml.parse_entries(["BareName"], "skin", "1.43")
    assert e["name"] == "BareName"
    assert e["kind"] == "skin"
    assert e["required_extensions"] == []


# ── get_extension_deps ───────────────────────────────────────────────────────

def _entry(name, deps=None):
    return {"name": name, "required_extensions": deps or []}


def test_get_extension_deps_orders_deps_first():
    entries = [_entry("Bootstrap"), _entry("BootstrapComponents", ["Bootstrap"])]
    resolved = parse_yaml.get_extension_deps("BootstrapComponents", entries)
    names = [e["name"] for e in resolved]
    assert names == ["Bootstrap", "BootstrapComponents"]


def test_get_extension_deps_transitive_and_dedup():
    entries = [
        _entry("A"),
        _entry("B", ["A"]),
        _entry("C", ["B", "A"]),
    ]
    names = [e["name"] for e in parse_yaml.get_extension_deps("C", entries)]
    assert names.index("A") < names.index("B") < names.index("C")
    assert names.count("A") == 1  # deduped even though C also lists A


def test_get_extension_deps_ignores_unknown_dep():
    entries = [_entry("X", ["DoesNotExist"])]
    names = [e["name"] for e in parse_yaml.get_extension_deps("X", entries)]
    assert names == ["X"]


# ── topological_sort ─────────────────────────────────────────────────────────

def test_topological_sort_respects_dependencies():
    entries = [_entry("B", ["A"]), _entry("A"), _entry("C", ["B"])]
    ordered = [e["name"] for e in parse_yaml.topological_sort(entries)]
    assert ordered.index("A") < ordered.index("B") < ordered.index("C")


def test_topological_sort_survives_cycle(capsys):
    entries = [_entry("A", ["B"]), _entry("B", ["A"])]
    ordered = [e["name"] for e in parse_yaml.topological_sort(entries)]
    # Both entries are still returned even though the cycle is unresolvable.
    assert set(ordered) == {"A", "B"}
    assert "Circular" in capsys.readouterr().err


# ── assign_batch ─────────────────────────────────────────────────────────────

def test_assign_batch_skin():
    assert parse_yaml.assign_batch(
        {"kind": "skin", "bundled": False, "name": "Modern"}
    ) == parse_yaml.BATCH_SKINS


def test_assign_batch_bundled_before_name():
    assert parse_yaml.assign_batch(
        {"kind": "extension", "bundled": True, "name": "Cite"}
    ) == parse_yaml.BATCH_BUNDLED


def test_assign_batch_smw_ecosystem():
    assert parse_yaml.assign_batch(
        {"kind": "extension", "bundled": False, "name": "SemanticMediaWiki"}
    ) == parse_yaml.BATCH_SMW


def test_assign_batch_standalone_split_on_first_letter():
    al = {"kind": "extension", "bundled": False, "name": "Arrays"}
    mz = {"kind": "extension", "bundled": False, "name": "Widgets"}
    assert parse_yaml.assign_batch(al) == parse_yaml.BATCH_STANDALONE_AL
    assert parse_yaml.assign_batch(mz) == parse_yaml.BATCH_STANDALONE_MZ
    # Boundary: 'L' goes to a-l, 'M' goes to m-z.
    assert parse_yaml.assign_batch(
        {"kind": "extension", "bundled": False, "name": "Lingo"}
    ) == parse_yaml.BATCH_STANDALONE_AL
    assert parse_yaml.assign_batch(
        {"kind": "extension", "bundled": False, "name": "Maps"}
    ) == parse_yaml.BATCH_STANDALONE_MZ


# ── load_skip_list ───────────────────────────────────────────────────────────

def _write_ci(tmp_path, skip_yaml):
    ci_dir = tmp_path / ".ci"
    ci_dir.mkdir()
    (ci_dir / "skip_list.yaml").write_text(skip_yaml)
    return str(ci_dir)


def test_load_skip_list_categorized(tmp_path):
    ci_dir = _write_ci(tmp_path, (
        "external_services:\n"
        "  - name: CirrusSearch\n    reason: Needs Elasticsearch\n"
        "upstream_test_compat:\n"
        "  - name: Cargo\n    reason: One failing test\n"
        "partial_test_compat:\n"
        "  - name: PageForms\n"
        "    reason: PFHelperFormActionTest fail\n"
        "    exclude_tests:\n"
        "      - tests/phpunit/integration/includes/PFHelperFormActionTest.php\n"
    ))
    data = parse_yaml.load_skip_list(ci_dir)
    assert data["external_services"] == {"CirrusSearch"}
    assert data["upstream_test_compat"] == {"Cargo"}
    assert data["partial_test_compat"] == {"PageForms": ["tests/phpunit/integration/includes/PFHelperFormActionTest.php"]}


def test_load_skip_list_missing_file_returns_empty(tmp_path):
    data = parse_yaml.load_skip_list(str(tmp_path))
    assert data == {"external_services": set(), "upstream_test_compat": set(), "partial_test_compat": {}}


# ── build_manifest (integration of the above) ────────────────────────────────

MANIFEST_YAML = """\
extensions:
- ExtA:
    commit: aaaaaaa
- ExtB:
    commit: bbbbbbb
    required extensions:
    - ExtA
- CirrusSearch:
    commit: ccccccc
- Cargo:
    branch: master
    commit: ddddddd
skins:
- MySkin:
    commit: eeeeeee
"""


def _write_manifest_yaml(tmp_path):
    p = tmp_path / "1.43.yaml"
    p.write_text(MANIFEST_YAML)
    return str(p)


def test_build_manifest_basic_shape(tmp_path):
    manifest = parse_yaml.build_manifest(_write_manifest_yaml(tmp_path))
    assert manifest["mw_version"] == "1.43"
    assert manifest["total_entries"] == 5
    names = {e["name"] for e in manifest["entries"]}
    assert names == {"ExtA", "ExtB", "CirrusSearch", "Cargo", "MySkin"}


def test_build_manifest_declared_deps(tmp_path):
    manifest = parse_yaml.build_manifest(_write_manifest_yaml(tmp_path))
    ext_b = next(e for e in manifest["entries"] if e["name"] == "ExtB")
    assert ext_b["declared_deps"] == ["ExtA"]


def test_build_manifest_external_service_skip(tmp_path):
    skip_data = {"external_services": {"CirrusSearch"}, "upstream_test_compat": set()}
    manifest = parse_yaml.build_manifest(_write_manifest_yaml(tmp_path), skip_data)
    cirrus = next(e for e in manifest["entries"] if e["name"] == "CirrusSearch")
    assert cirrus["skip"] is True
    assert cirrus["skip_category"] == "external_services"
    assert manifest["skipped_count"] == 1


def test_build_manifest_upstream_compat_is_validated_not_skipped(tmp_path):
    skip_data = {"external_services": set(), "upstream_test_compat": {"Cargo"}}
    manifest = parse_yaml.build_manifest(_write_manifest_yaml(tmp_path), skip_data)
    cargo = next(e for e in manifest["entries"] if e["name"] == "Cargo")
    assert cargo["skip"] is False
    assert cargo["skip_tests"] is True
    assert cargo["skip_category"] == "upstream_test_compat"


def test_build_manifest_transitive_skip(tmp_path):
    # ExtA is an external-service skip; ExtB depends on it and must be skipped too.
    skip_data = {"external_services": {"ExtA"}, "upstream_test_compat": set()}
    manifest = parse_yaml.build_manifest(_write_manifest_yaml(tmp_path), skip_data)
    ext_b = next(e for e in manifest["entries"] if e["name"] == "ExtB")
    assert ext_b["skip"] is True
    assert ext_b["skip_category"] == "transitive"


def test_build_manifest_partial_test_compat(tmp_path):
    skip_data = {
        "external_services": set(),
        "upstream_test_compat": set(),
        "partial_test_compat": {"Cargo": ["tests/phpunit/integration/formats/CargoFeedFormatTest.php"]}
    }
    manifest = parse_yaml.build_manifest(_write_manifest_yaml(tmp_path), skip_data)
    cargo = next(e for e in manifest["entries"] if e["name"] == "Cargo")
    assert cargo["skip"] is False
    assert cargo.get("skip_tests") is not True
    assert cargo["exclude_tests"] == ["tests/phpunit/integration/formats/CargoFeedFormatTest.php"]
    assert cargo["skip_category"] == "partial_test_compat"


# ── _is_transient_git_error ──────────────────────────────────────────────────

class TestIsTransientGitError:
    """Verify that the transient-error detector matches known patterns."""

    @pytest.mark.parametrize("stderr", [
        "fatal: unable to access 'https://github.com/…/': The requested URL returned error: 503",
        "error: The requested URL returned error: 429",
        "fatal: unable to access 'https://…': Could not resolve host: github.com",
        "fatal: unable to access 'https://…': connection reset by peer",
        "fatal: unable to access 'https://…': Connection timed out",
        "fatal: unable to access 'https://…': SSL connection timeout",
        "fatal: unable to access 'https://…': Failed to connect to github.com",
        "fatal: unable to access 'https://…': Connection refused",
        "fatal: unable to access 'https://…': Network is unreachable",
        "fatal: unable to access 'https://…': gnutls_handshake() failed: TLS error",
    ])
    def test_transient_patterns_match(self, stderr):
        assert parse_yaml._is_transient_git_error(stderr) is True

    @pytest.mark.parametrize("stderr", [
        "fatal: couldn't find remote ref refs/heads/REL1_99",
        "error: no such remote ref abcdef1234567890",
        "fatal: not our ref abcdef1234567890",
        # SHA containing '503' as substring (false positive check)
        "fatal: remote error: upload-pack: not our ref a1b2503c4d5e",
        # SHA containing '429' as substring (false positive check)
        "fatal: remote error: upload-pack: not our ref a1b2429c4d5e",
        # Generic HTTP prefix with a 404 (non-transient check)
        "fatal: unable to access 'https://github.com/x/y.git/': The requested URL returned error: 404",
        "",
    ])
    def test_non_transient_patterns_do_not_match(self, stderr):
        assert parse_yaml._is_transient_git_error(stderr) is False


# ── _run_git_with_retry ──────────────────────────────────────────────────────


def _make_result(returncode, stderr=""):
    """Create a fake subprocess.CompletedProcess."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


class TestRunGitWithRetry:
    """Verify exponential-backoff retry logic in _run_git_with_retry."""

    @patch("parse_yaml.subprocess.run")
    def test_success_on_first_try(self, mock_run):
        mock_run.return_value = _make_result(0)
        result = parse_yaml._run_git_with_retry(
            ["git", "ls-remote", "https://example.com"],
            max_retries=3, _sleep_fn=lambda _: None,
        )
        assert result.returncode == 0
        assert mock_run.call_count == 1

    @patch("parse_yaml.subprocess.run")
    def test_retries_on_transient_then_succeeds(self, mock_run):
        mock_run.side_effect = [
            _make_result(128, "fatal: unable to access '…': The requested URL returned error: 503"),
            _make_result(128, "fatal: unable to access '…': The requested URL returned error: 503"),
            _make_result(0),
        ]
        sleeps = []
        result = parse_yaml._run_git_with_retry(
            ["git", "ls-remote", "https://example.com"],
            max_retries=3, base_delay=1, _sleep_fn=sleeps.append,
        )
        assert result.returncode == 0
        assert mock_run.call_count == 3
        # Exponential backoff: 1*2^0=1, 1*2^1=2
        assert sleeps == [1, 2]

    @patch("parse_yaml.subprocess.run")
    def test_gives_up_after_max_retries(self, mock_run):
        transient = _make_result(128, "fatal: unable to access '…': The requested URL returned error: 503")
        mock_run.return_value = transient
        result = parse_yaml._run_git_with_retry(
            ["git", "ls-remote", "https://example.com"],
            max_retries=2, base_delay=1, _sleep_fn=lambda _: None,
        )
        assert result.returncode == 128
        # 1 initial + 2 retries = 3 total attempts
        assert mock_run.call_count == 3

    @patch("parse_yaml.subprocess.run")
    def test_non_transient_error_fails_immediately(self, mock_run):
        mock_run.return_value = _make_result(128, "fatal: not our ref abcdef")
        result = parse_yaml._run_git_with_retry(
            ["git", "ls-remote", "https://example.com"],
            max_retries=3, _sleep_fn=lambda _: None,
        )
        assert result.returncode == 128
        # No retry for genuine errors.
        assert mock_run.call_count == 1

    @patch("parse_yaml.subprocess.run")
    def test_timeout_propagates(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=60)
        with pytest.raises(subprocess.TimeoutExpired):
            parse_yaml._run_git_with_retry(
                ["git", "fetch", "https://example.com"],
                max_retries=3, _sleep_fn=lambda _: None,
            )
        assert mock_run.call_count == 4


# ── validate_commits retry integration ───────────────────────────────────────

class TestValidateCommitsRetry:
    """Verify that validate_commits retries transient errors end-to-end."""

    def _entry(self, name="TestExt", commit="abcdef123456", branch="REL1_43",
               repo="https://github.com/example/repo"):
        return {
            "name": name,
            "kind": "extension",
            "bundled": False,
            "repository": repo,
            "branch": branch,
            "commit": commit,
        }

    @patch("parse_yaml.subprocess.run")
    def test_transient_branch_check_is_retried(self, mock_run):
        """Branch check uses _run_git_with_retry so transient errors get retried."""
        # First call (git ls-remote) has transient error, second call succeeds.
        # Third call (git init) succeeds.
        # Fourth call (git fetch) succeeds.
        mock_run.side_effect = [
            _make_result(128, "fatal: unable to access '…': The requested URL returned error: 503"),
            _make_result(0),
            _make_result(0),  # git init
            _make_result(0),  # git fetch
        ]

        entries = [self._entry()]
        with patch("time.sleep") as mock_sleep:
            failures = parse_yaml.validate_commits(entries)
        assert failures == []
        assert mock_sleep.call_count == 1
        # 1 failed + 1 successful ls-remote + 1 init + 1 fetch = 4 runs
        assert mock_run.call_count == 4

    @patch("parse_yaml.subprocess.run")
    def test_genuine_missing_branch_fails_immediately(self, mock_run):
        """A genuine missing branch (non-transient) fails without retrying."""
        mock_run.side_effect = [
            _make_result(2, "fatal: couldn't find remote ref refs/heads/REL1_99"),
        ]

        entries = [self._entry(branch="REL1_99")]
        with patch("time.sleep") as mock_sleep:
            failures = parse_yaml.validate_commits(entries)
        assert len(failures) == 1
        assert "REL1_99" in failures[0]
        # Should not sleep because there are no retries
        assert mock_sleep.call_count == 0
        # Should have called subprocess.run only once for the ls-remote command
        assert mock_run.call_count == 1

    @patch("parse_yaml.subprocess.run")
    def test_circuit_breaker_stops_retries(self, mock_run):
        """After _CONSECUTIVE_TRANSIENT_LIMIT transient failures, subsequent checks skip immediately."""
        # 3 consecutive transient failures across different entries:
        # Ext1 ls-remote: transient (retried, fails) -> consecutive_transient = 1
        # Ext2 ls-remote: transient (retried, fails) -> consecutive_transient = 2
        # Ext3 ls-remote: transient (retried, fails) -> consecutive_transient = 3
        # Aborts immediately. Ext4 is never checked or even appended as a skipped entry.
        transient_error = _make_result(128, "fatal: unable to access '…': Could not resolve host")
        mock_run.return_value = transient_error

        entries = [
            self._entry(name="Ext1"),
            self._entry(name="Ext2"),
            self._entry(name="Ext3"),
            self._entry(name="Ext4"),
        ]
        with patch("time.sleep") as mock_sleep:
            failures = parse_yaml.validate_commits(entries)

        assert len(failures) == 4
        assert "Ext1" in failures[0]
        assert "Ext2" in failures[1]
        assert "Ext3" in failures[2]
        assert "Validation aborted" in failures[3]
        assert "consecutive transient failures" in failures[3]

        # Ext1, Ext2, Ext3 are each checked 4 times (1 initial + 3 retries) -> 12 calls total.
        # Ext4 should not call subprocess.run at all.
        assert mock_run.call_count == 12
        # Each failed entry has 3 retries (thus 3 sleeps) -> 9 sleeps total.
        assert mock_sleep.call_count == 9

    @pytest.mark.parametrize("stderr", [
        "fatal: the remote end hung up unexpectedly",
        "error: RPC failed; curl 18 transfer closed with outstanding read data remaining",
        "fatal: early EOF",
        "fatal: unable to access 'https://github.com/foo': Empty reply from server",
        "fatal: unable to access 'https://github.com/foo': Error in the HTTP2 framing layer",
    ])
    def test_new_transient_patterns_match(self, stderr):
        assert parse_yaml._is_transient_git_error(stderr) is True

    @pytest.mark.parametrize("stderr", [
        "fatal: couldn't find remote ref refs/heads/REL1_43 in .../mediawiki-extensions-TLSAuth",
        "error: Server does not allow request for unadvertised object 1234abcd; ssl-cache",
    ])
    def test_ssl_tls_anchoring_false_positives(self, stderr):
        assert parse_yaml._is_transient_git_error(stderr) is False

    @patch("parse_yaml.subprocess.run")
    def test_validate_commits_time_budget(self, mock_run):
        mock_run.return_value = _make_result(0)
        entries = [
            self._entry(name="Ext1"),
            self._entry(name="Ext2"),
        ]
        # Simulate time budget exceeded immediately
        with patch("time.monotonic", side_effect=[0.0, 601.0, 602.0]):
            failures = parse_yaml.validate_commits(entries)

        assert len(failures) == 1
        assert "exceeded total elapsed time limit of 600s" in failures[0]

