"""Unit tests for scripts/pin_updates.py.

These cover the pure logic — noise classification, MediaWiki constraint
checks, version comparison, issue-body rendering and parsing, and YAML
rewriting — without cloning any repositories.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pin_updates  # noqa: E402


SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
WEB = "https://github.com/example/ext"


# ── is_noise ─────────────────────────────────────────────────────────────────

def test_translation_updates_are_noise():
    assert pin_updates.is_noise(
        "Localisation updates from https://translatewiki.net.", ["i18n/de.json"])


def test_test_and_ci_only_changes_are_noise():
    assert pin_updates.is_noise("Fix flaky test", ["tests/phpunit/FooTest.php"])
    assert pin_updates.is_noise("Update workflow", [".github/workflows/ci.yml"])


def test_library_upgrader_commits_are_noise():
    assert pin_updates.is_noise("build: Updating mediawiki/mediawiki-codesniffer to 47.0.0",
                                ["composer.json"])


def test_code_changes_are_significant():
    assert not pin_updates.is_noise("SECURITY: Sanitize links", ["includes/Hooks.php"])


def test_mixed_noise_and_code_is_significant():
    assert not pin_updates.is_noise("Fix bug", ["i18n/en.json", "includes/Foo.php"])


# ── satisfies ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("constraint, expected", [
    (None, True),
    ("*", True),
    (">= 1.43.0", True),
    (">=1.43", True),
    (">= 1.43.9", True),
    (">= 1.43.12", False),
    (">= 1.46.0", False),
    (">= 1.39 < 1.45", True),
    (">= 1.39, < 1.43", False),
    ("^1.43", True),
    ("1.43", True),
    ("1.44", False),
    (">= 1.46 || >= 1.43", True),
])
def test_satisfies(constraint, expected):
    assert pin_updates.satisfies("1.43.11", constraint) is expected


# ── versions ─────────────────────────────────────────────────────────────────

def test_version_label_strips_leading_v():
    assert pin_updates.version_label("v0.14.0") == "0.14.0"
    assert pin_updates.version_label("2.4.3") == "2.4.3"
    assert pin_updates.version_label(None) is None


@pytest.mark.parametrize("old, new, expected", [
    ("6.0.0", "7.3.1", True),
    ("0.9", "1.0", True),
    ("2.3.1", "2.4.3", False),
    ("5.0.0-dev", "6.0.2", True),
    (None, "2.0", False),
])
def test_is_major_bump(old, new, expected):
    assert pin_updates.is_major_bump(old, new) is expected


def test_web_url_uses_github_mirror_for_gerrit():
    entry = {"name": "Cargo", "kind": "extension",
             "repository": "https://gerrit.wikimedia.org/r/mediawiki/extensions/Cargo"}
    assert pin_updates.web_url(entry) == \
        "https://github.com/wikimedia/mediawiki-extensions-Cargo"


def test_web_url_for_github_repo():
    entry = {"name": "AWS", "kind": "extension",
             "repository": "https://github.com/edwardspec/mediawiki-aws-s3.git"}
    assert pin_updates.web_url(entry) == "https://github.com/edwardspec/mediawiki-aws-s3"


def test_web_url_for_other_hosts_is_none():
    entry = {"name": "X", "kind": "extension", "repository": "https://example.com/x.git"}
    assert pin_updates.web_url(entry) is None


# ── rendering and parsing the issue body ─────────────────────────────────────

def candidate(name, category, new=SHA_B, tagged=True, kind="extension"):
    return {
        "kind": kind, "name": name, "branch": "master", "old": SHA_A, "new": new,
        "old_version": "1.0", "new_version": "2.0" if category == "major" else "1.1",
        "tagged": tagged, "category": category, "web": WEB,
        "commits": [{"sha": SHA_B, "date": "2026-10-01", "subject": "Fix <thing>",
                     "significant": True}],
    }


def test_defaults_tick_everything_but_major_bumps():
    body = pin_updates.render_body(
        "1.43.yaml", [candidate("Minor", "release"), candidate("Big", "major")],
        [], "1.43.11", SHA_C, today="2026-11-02")
    names = [u["name"] for u in pin_updates.ticked(body)]
    assert names == ["Minor"]
    assert pin_updates.body_yaml_file(body) == "1.43.yaml"
    assert "Fix &lt;thing>" in body
    assert f"[`bbbbbbb`]({WEB}/commit/{SHA_B[:12]}) Fix" in body
    assert f"[compare]({WEB}/compare/{SHA_A[:12]}...{SHA_B[:12]})" in body


def test_truncated_commit_lists_link_to_the_details_page():
    c = candidate("Busy", "release")
    c["commits"] *= 12
    run = "https://github.com/o/r/actions/runs/1"
    body = pin_updates.render_body("1.43.yaml", [c], [], "1.43.11", SHA_C, details_url=run)
    assert f"[… and 2 more]({run})" in body


def test_truncated_commit_lists_fall_back_to_the_comparison():
    c = candidate("Busy", "release")
    c["commits"] *= 12
    body = pin_updates.render_body("1.43.yaml", [c], [], "1.43.11", SHA_C)
    assert f"[… and 2 more]({WEB}/compare/{SHA_A[:12]}...{SHA_B[:12]})" in body


def test_details_list_every_significant_commit_without_checkboxes():
    c = candidate("Busy", "release")
    noise = {"sha": SHA_C, "date": "2026-10-02", "subject": "Translations",
             "significant": False}
    c["commits"] = c["commits"] * 30 + [noise]
    details = pin_updates.render_details("1.43.yaml", [c], "1.43.11", SHA_C)
    assert details.count(f"/commit/{SHA_B[:12]}") == 30
    assert "Translations" not in details
    assert "- [" not in details.replace("- [`", "")
    assert "<!-- pin" not in details
    assert pin_updates.ticked(details) == []


def test_previous_ticks_are_kept_for_the_same_revision():
    first = pin_updates.render_body("1.43.yaml", [candidate("Big", "major")], [],
                                    "1.43.11", SHA_C)
    edited = first.replace("- [ ] **Big**", "- [x] **Big**")
    again = pin_updates.render_body("1.43.yaml", [candidate("Big", "major")], [],
                                    "1.43.11", SHA_C, previous=edited)
    assert [u["name"] for u in pin_updates.ticked(again)] == ["Big"]


def test_previous_ticks_are_dropped_when_the_revision_changes():
    first = pin_updates.render_body("1.43.yaml", [candidate("Minor", "release")], [],
                                    "1.43.11", SHA_C)
    edited = first.replace("- [x] **Minor**", "- [ ] **Minor**")
    again = pin_updates.render_body("1.43.yaml", [candidate("Minor", "release", new=SHA_C)],
                                    [], "1.43.11", SHA_C, previous=edited)
    assert [u["name"] for u in pin_updates.ticked(again)] == ["Minor"]


def test_body_stays_under_the_github_limit():
    many = [candidate(f"Ext{i}", "release") for i in range(400)]
    for c in many:
        c["commits"] *= 50
    body = pin_updates.render_body("1.43.yaml", many, [], "1.43.11", SHA_C)
    assert "<details>" not in body or len(body) <= pin_updates.MAX_BODY_CHARS


def test_ticked_rejects_a_malformed_commit():
    body = '- [x] **X** <!-- pin {"kind":"extension","name":"X","commit":"main"} -->\n'
    with pytest.raises(ValueError):
        pin_updates.ticked(body)


# ── apply_updates ────────────────────────────────────────────────────────────

YAML_TEXT = f"""\
extensions:
- Cargo:
    branch: master
    commit: {SHA_A} # v. 3.9.4
- CharInsert:
    commit: {SHA_A}
skins:
- Cargo:
    commit: {SHA_A}
"""


def test_apply_updates_rewrites_only_the_selected_entry():
    text, changes = pin_updates.apply_updates(YAML_TEXT, [
        {"kind": "extension", "name": "Cargo", "commit": SHA_B,
         "version": None, "declared": "3.9.5"},
    ])
    assert f"commit: {SHA_B} # v. 3.9.5" in text
    assert text.count(SHA_A) == 2
    assert changes == ["Cargo: aaaaaaa → bbbbbbb (3.9.5)"]


def test_apply_updates_keeps_skins_and_extensions_apart():
    text, _ = pin_updates.apply_updates(YAML_TEXT, [
        {"kind": "skin", "name": "Cargo", "commit": SHA_C, "version": None,
         "declared": "1.0"},
    ])
    assert text.endswith(f"commit: {SHA_C}\n")
    assert f"commit: {SHA_A} # v. 3.9.4" in text


def test_apply_updates_adds_a_comment_only_for_tags():
    text, _ = pin_updates.apply_updates(YAML_TEXT, [
        {"kind": "extension", "name": "CharInsert", "commit": SHA_B,
         "version": None, "declared": "1.0"},
    ])
    assert f"    commit: {SHA_B}\n" in text
    text, _ = pin_updates.apply_updates(YAML_TEXT, [
        {"kind": "extension", "name": "CharInsert", "commit": SHA_B,
         "version": "2.0", "declared": "2.0"},
    ])
    assert f"    commit: {SHA_B} # v. 2.0\n" in text


def test_apply_updates_fails_for_an_unknown_entry():
    with pytest.raises(ValueError):
        pin_updates.apply_updates(YAML_TEXT, [
            {"kind": "extension", "name": "Nope", "commit": SHA_B},
        ])


def test_pin_comments_reads_versions():
    assert pin_updates.pin_comments(YAML_TEXT) == {("extension", "Cargo"): "3.9.4"}
