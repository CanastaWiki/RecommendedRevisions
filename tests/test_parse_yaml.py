"""Unit tests for scripts/parse_yaml.py.

These cover the pure logic that drives the CI pipeline — version extraction,
branch/URL derivation, entry parsing, dependency resolution, batch assignment,
skip-list categorization, and the transitive-skip loop in build_manifest —
without needing a MediaWiki install or network access.
"""

import os
import sys

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
    ))
    data = parse_yaml.load_skip_list(ci_dir)
    assert data["external_services"] == {"CirrusSearch"}
    assert data["upstream_test_compat"] == {"Cargo"}


def test_load_skip_list_missing_file_returns_empty(tmp_path):
    data = parse_yaml.load_skip_list(str(tmp_path))
    assert data == {"external_services": set(), "upstream_test_compat": set()}


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
