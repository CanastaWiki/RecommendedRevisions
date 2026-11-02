#!/usr/bin/env python3
"""
pin_updates.py — Find newer revisions for pinned extensions and skins, and
apply the ones a maintainer selects.

Usage:
    python pin_updates.py scan 1.43.yaml --output body.md [--previous old.md]
    python pin_updates.py apply 1.43.yaml --body body.md [--summary pr.md]

`scan` clones every pinned repository (blobless, into a cache directory),
looks for significant commits between the pinned commit and the tracked
branch, and writes a Markdown checklist suitable for a GitHub issue body.
Passing the current issue body as --previous keeps the maintainer's ticks
for candidates that have not changed.

`apply` reads ticked entries from an issue body and rewrites the commit
lines (and version comments) in the YAML file in place.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from parse_yaml import extract_mw_version, parse_entries  # noqa: E402


USER_AGENT = "RecommendedRevisions-pin-updates (https://github.com/CanastaWiki/RecommendedRevisions)"
MW_CORE_REPO = "https://gerrit.wikimedia.org/r/mediawiki/core"

# GitHub caps issue bodies at 65,536 characters.
MAX_BODY_CHARS = 64000

SECTIONS = [
    ("maintenance", "Maintenance branch updates", True),
    ("release", "New releases", True),
    ("untagged", "Untagged changes", True),
    ("major", "Major version bumps", False),
]

# A commit is noise when every file it touches matches one of these.
NOISE_PATH = re.compile(
    r"^(i18n/|tests?/|\.github/|\.phan/|\.ci/|docs?/|\.gitreview$|\.eslintrc|"
    r"\.stylelintrc|\.?phpcs\.xml$|phpunit\.xml|\.mailmap$|Gruntfile\.js$|"
    r"package(-lock)?\.json$|\.npmrc$|\.nvmrc$|CODE_OF_CONDUCT|\.gitignore$|"
    r"\.gitattributes$|\.editorconfig$|\.scrutinizer|\.travis|Doxyfile|"
    r"\.jshintrc|\.jsonlint|\.codeclimate|codecov|Makefile$|psalm\.xml|"
    r"phpstan|\.prettierrc|\.banana|COPYING|AUTHORS|composer\.lock$|"
    r".*\.md$|\.coderabbit|\.vscode|\.idea)"
)

# ...or when its subject matches one of these.
NOISE_SUBJECT = re.compile(
    r"^(Localisation updates from https://translatewiki|build: |"
    r"Update git submodules|Bump .* from |ci:|chore|tests?:|docs(\(.*\))?:|"
    r"deps\(build|\[CI\]|Add \.phan|"
    r".*\b(phan|phpcs|codesniffer)\b|"
    r"(Prepare|Release|Version)\b.*\d|"
    r"Update composer\.json authors|Add .* to authors)",
    re.I,
)

PRERELEASE = re.compile(r"(alpha|beta|rc|dev|pre)", re.I)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MARKER_RE = re.compile(r"^- \[([ xX])\] .*<!-- pin (\{.*\}) -->\s*$")
FILE_MARKER = "<!-- pin-updates-file: {} -->"


# ── Pure helpers ─────────────────────────────────────────────────────────────

def is_noise(subject: str, files: list[str]) -> bool:
    """Return True for commits that do not change what a wiki runs."""
    if NOISE_SUBJECT.match(subject):
        return True
    return all(NOISE_PATH.match(f) for f in files)


def parse_version(text: str | None) -> tuple[int, ...] | None:
    """'v2.4.3' -> (2, 4, 3); None when there is no leading number."""
    if not text:
        return None
    match = re.match(r"v?(\d+(?:\.\d+)*)", text.strip())
    if not match:
        return None
    return tuple(int(p) for p in match.group(1).split("."))


def _cmp(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    width = max(len(a), len(b))
    a = a + (0,) * (width - len(a))
    b = b + (0,) * (width - len(b))
    return (a > b) - (a < b)


def satisfies(version: str, constraint: str | None) -> bool:
    """
    Check a Composer-style constraint such as '>= 1.43.0', '>=1.39 <1.45',
    '^1.43' or '>= 1.39 || >= 1.43'.  Unknown syntax is treated as satisfied
    so that an odd constraint never hides an update.
    """
    if not constraint or constraint.strip() in ("*", ""):
        return True
    have = parse_version(version)
    for alternative in constraint.split("||"):
        ok = True
        for op, ver in re.findall(r"(>=|<=|>|<|==|=|\^|~|!=)?\s*v?(\d+(?:\.\d+)*)",
                                  alternative.replace(",", " ")):
            want = parse_version(ver)
            c = _cmp(have, want)
            if op in ("", "=", "=="):
                # A bare '1.43' allows any 1.43.x.
                ok &= have[:len(want)] == want
            elif op == ">=":
                ok &= c >= 0
            elif op == ">":
                ok &= c > 0
            elif op == "<=":
                ok &= c <= 0
            elif op == "<":
                ok &= c < 0
            elif op == "!=":
                ok &= c != 0
            elif op == "^":
                ok &= c >= 0 and have[0] == want[0]
            elif op == "~":
                ok &= c >= 0 and have[:max(1, len(want) - 1)] == want[:max(1, len(want) - 1)]
        if ok:
            return True
    return False


def version_label(text: str | None) -> str | None:
    """Tag or version string as written in YAML comments: 'v2.4.3' -> '2.4.3'."""
    if not text:
        return None
    return re.sub(r"^v(?=\d)", "", text.strip())


def is_major_bump(old: str | None, new: str | None) -> bool:
    a, b = parse_version(old), parse_version(new)
    if not a or not b:
        return False
    return b[0] > a[0]


def web_url(entry: dict) -> str | None:
    """GitHub page for the repository; Gerrit repositories use their GitHub mirror."""
    repo = entry["repository"]
    if repo.startswith("https://gerrit.wikimedia.org/r/mediawiki/"):
        kind = "extensions" if entry["kind"] == "extension" else "skins"
        return f"https://github.com/wikimedia/mediawiki-{kind}-{entry['name']}"
    if repo.startswith("https://github.com/"):
        return repo.removesuffix(".git").rstrip("/")
    return None


# ── Git access ───────────────────────────────────────────────────────────────

def git(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", f"http.userAgent={USER_AGENT}"] + args,
        cwd=cwd, capture_output=True, text=True, timeout=900,
    )


def latest_mw_release(mw_version: str) -> str:
    """Newest MediaWiki x.y.z tag for the branch, e.g. '1.43.11'."""
    out = git(["ls-remote", "--tags", MW_CORE_REPO, f"refs/tags/{mw_version}.*"]).stdout
    tags = {line.split("refs/tags/")[1].removesuffix("^{}") for line in out.splitlines()}
    releases = [t for t in tags if re.fullmatch(re.escape(mw_version) + r"\.\d+", t)]
    if not releases:
        return mw_version + ".0"
    return max(releases, key=parse_version)


class Repo:
    """A blobless bare clone of one extension or skin repository."""

    def __init__(self, path: str):
        self.path = path

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return git(list(args), cwd=self.path)

    def out(self, *args: str) -> str:
        return self.run(*args).stdout.strip()

    def manifest(self, rev: str) -> dict:
        for name in ("extension.json", "skin.json"):
            result = self.run("show", f"{rev}:{name}")
            if result.returncode == 0:
                try:
                    return json.loads(result.stdout)
                except ValueError:
                    return {}
        return {}

    def requires_mw(self, rev: str) -> str | None:
        return self.manifest(rev).get("requires", {}).get("MediaWiki")

    def declared_version(self, rev: str) -> str | None:
        return self.manifest(rev).get("version")

    def exact_tag(self, rev: str) -> str | None:
        tags = self.out("tag", "--points-at", rev).split()
        stable = [t for t in tags if not PRERELEASE.search(t)]
        return max(stable, key=lambda t: parse_version(t) or ()) if stable else None

    def is_ancestor(self, a: str, b: str) -> bool:
        return self.run("merge-base", "--is-ancestor", a, b).returncode == 0


def open_repo(entry: dict, cache: str) -> Repo:
    path = os.path.join(cache, entry["kind"], entry["name"])
    if os.path.isdir(path):
        result = git(["fetch", "-q", "--prune", "--tags", "origin",
                      "+refs/heads/*:refs/heads/*"], cwd=path)
    else:
        result = git(["clone", "-q", "--bare", "--filter=blob:none",
                      entry["repository"], path])
    if result.returncode:
        raise RuntimeError(result.stderr.strip().splitlines()[-1] if result.stderr.strip()
                           else "git failed")
    return Repo(path)


# ── Scanning ─────────────────────────────────────────────────────────────────

def newest_compatible(repo: Repo, pin: str, rev: str, mw_release: str) -> str | None:
    """
    Walk back from rev to the newest commit whose manifest still accepts
    mw_release.  Only commits that touch the manifest can change the answer,
    so step past those.
    """
    while rev != pin:
        if satisfies(mw_release, repo.requires_mw(rev)):
            return rev
        changed = repo.out("log", "-1", "--format=%H", f"{pin}..{rev}",
                           "--", "extension.json", "skin.json")
        if not changed:
            return None
        rev = repo.out("rev-parse", f"{changed}^")
        if not rev or not repo.is_ancestor(pin, rev):
            return None
    return None


def commits_between(repo: Repo, old: str, new: str) -> list[dict]:
    log = repo.out("log", "--no-merges", "--format=%x00%H%x01%cs%x01%s",
                   "--name-only", f"{old}..{new}")
    commits = []
    for chunk in log.split("\x00")[1:]:
        header, *rest = chunk.split("\n")
        sha, date, subject = header.split("\x01", 2)
        files = [f for f in rest if f.strip()]
        commits.append({"sha": sha, "date": date, "subject": subject,
                        "significant": not is_noise(subject, files)})
    return commits


def pin_comments(yaml_text: str) -> dict[tuple[str, str], str]:
    """Map (kind, name) to the version written after its commit line."""
    comments = {}
    kind = name = None
    for line in yaml_text.splitlines():
        if line in ("extensions:", "skins:"):
            kind = line[:-2]
        elif m := re.match(r"^- ([^:]+):\s*$", line):
            name = m.group(1)
        elif m := re.match(r"^\s+commit:\s*[0-9a-f]+\s*#\s*v\.?\s*(\S+)", line):
            comments[(kind, name)] = m.group(1)
    return comments


def scan_entry(entry: dict, cache: str, mw_version: str, mw_release: str,
               pinned_version: str | None) -> dict | None:
    repo = open_repo(entry, cache)
    pin = entry["commit"]
    branch = entry["branch"] or repo.out("symbolic-ref", "--short", "HEAD")
    head = repo.out("rev-parse", "--verify", f"refs/heads/{branch}")
    if not head:
        raise RuntimeError(f"branch {branch} not found")
    if not repo.is_ancestor(pin, head):
        raise RuntimeError(f"pinned commit is not on {branch}")
    if head == pin:
        return None

    maintenance = branch == "REL" + mw_version.replace(".", "_")
    target = None
    if not maintenance:
        tags = repo.out("tag", "--merged", head, "--no-merged", pin,
                        "--sort=-v:refname").split()
        for tag in (t for t in tags if not PRERELEASE.search(t)):
            rev = repo.out("rev-parse", f"{tag}^{{commit}}")
            if satisfies(mw_release, repo.requires_mw(rev)):
                target = rev
                break
    if target is None:
        target = newest_compatible(repo, pin, head, mw_release)
    if target is None or target == pin:
        return None

    commits = commits_between(repo, pin, target)
    if not any(c["significant"] for c in commits):
        return None

    tag = repo.exact_tag(target)
    new_version = version_label(tag) or repo.declared_version(target)
    old_version = pinned_version or version_label(repo.exact_tag(pin)) \
        or repo.declared_version(pin)
    if maintenance:
        category = "maintenance"
    elif is_major_bump(old_version, new_version):
        category = "major"
    elif tag:
        category = "release"
    else:
        category = "untagged"

    return {
        "kind": entry["kind"],
        "name": entry["name"],
        "branch": branch,
        "old": pin,
        "new": target,
        "old_version": old_version,
        "new_version": new_version,
        "tagged": bool(tag),
        "category": category,
        "web": web_url(entry),
        "commits": commits,
    }


def scan(yaml_path: str, cache: str, jobs: int = 12) -> tuple[list[dict], list[str], str]:
    mw_version = extract_mw_version(yaml_path)
    text = open(yaml_path).read()
    data = yaml.safe_load(text)
    entries = (parse_entries(data.get("extensions") or [], "extension", mw_version)
               + parse_entries(data.get("skins") or [], "skin", mw_version))
    entries = [e for e in entries if not e["bundled"] and e["commit"]]
    comments = pin_comments(text)
    mw_release = latest_mw_release(mw_version)

    def work(entry):
        key = (entry["kind"], entry["name"])
        try:
            return scan_entry(entry, cache, mw_version, mw_release, comments.get(key)), None
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            return None, f"{entry['name']}: {exc}"

    with ThreadPoolExecutor(jobs) as pool:
        results = list(pool.map(work, entries))
    candidates = [r for r, _ in results if r]
    errors = [e for _, e in results if e]
    return candidates, errors, mw_release


# ── Issue body ───────────────────────────────────────────────────────────────

def previous_ticks(body: str) -> dict[tuple[str, str, str], bool]:
    ticks = {}
    for line in body.splitlines():
        if m := MARKER_RE.match(line):
            data = json.loads(m.group(2))
            ticks[(data["kind"], data["name"], data["commit"])] = m.group(1) != " "
    return ticks


def _short(sha: str) -> str:
    return sha[:7]


def _abbrev(sha: str) -> str:
    """Abbreviation for URLs; long enough to stay unambiguous."""
    return sha[:12]


def _change(c: dict) -> str:
    if c["tagged"]:
        return f"{c['old_version'] or _short(c['old'])} → {c['new_version']}"
    change = f"`{_short(c['old'])}` → `{_short(c['new'])}`"
    if c["new_version"] and c["new_version"] != c["old_version"]:
        change += f" (version {c['old_version'] or '?'} → {c['new_version']})"
    return change


def _compare(c: dict) -> str | None:
    web = c["web"]
    return f"{web}/compare/{_abbrev(c['old'])}...{_abbrev(c['new'])}" if web else None


def _commit_line(c: dict, x: dict, indent: str) -> str:
    subject = x["subject"].replace("<", "&lt;")
    if len(subject) > 90:
        subject = subject[:87] + "..."
    web = c["web"]
    sha = f"[`{_short(x['sha'])}`]({web}/commit/{_abbrev(x['sha'])})" if web \
        else f"`{_short(x['sha'])}`"
    return f"{indent}- {x['date']} {sha} {subject}"


def _summary(c: dict) -> str:
    significant = sum(1 for x in c["commits"] if x["significant"])
    compare = _compare(c)
    link = f" · [compare]({compare})" if compare else ""
    return (f"**{c['name']}** ({c['kind']}, `{c['branch']}`) {_change(c)} · "
            f"{significant} significant of {len(c['commits'])} commits{link}")


def _entry_lines(c: dict, checked: bool, max_commits: int,
                 details_url: str | None = None) -> list[str]:
    significant = [x for x in c["commits"] if x["significant"]]
    marker = json.dumps({"kind": c["kind"], "name": c["name"], "commit": c["new"],
                         "version": c["new_version"] if c["tagged"] else None,
                         "declared": c["new_version"]},
                        separators=(",", ":"))
    lines = [f"- [{'x' if checked else ' '}] {_summary(c)} <!-- pin {marker} -->"]
    if max_commits:
        lines += ["  <details><summary>Significant commits</summary>", ""]
        lines += [_commit_line(c, x, "  ") for x in significant[:max_commits]]
        if len(significant) > max_commits:
            more = f"… and {len(significant) - max_commits} more"
            target = details_url or _compare(c)
            lines.append(f"  - [{more}]({target})" if target else f"  - {more}")
        lines += ["", "  </details>"]
    return lines


def _sorted(candidates: list[dict]) -> list[dict]:
    order = {key: i for i, (key, _, _) in enumerate(SECTIONS)}
    return sorted(candidates, key=lambda c: (order[c["category"]], c["name"].lower()))


def render_details(yaml_file: str, candidates: list[dict], mw_release: str,
                   source_sha: str, today: str | None = None) -> str:
    """Every significant commit for every candidate, without checkboxes."""
    today = today or datetime.date.today().isoformat()
    candidates = _sorted(candidates)
    lines = [
        f"## Significant commits for `{yaml_file}`",
        "",
        f"Scanned {today} against {source_sha[:7]} for MediaWiki {mw_release}. "
        "Choose updates in the pin updates issue; this page is for reading only.",
    ]
    for key, title, _ in SECTIONS:
        section = [c for c in candidates if c["category"] == key]
        if not section:
            continue
        lines += ["", f"### {title} ({len(section)})"]
        for c in section:
            lines += ["", _summary(c), ""]
            lines += [_commit_line(c, x, "") for x in c["commits"] if x["significant"]]
    return "\n".join(lines) + "\n"


def render_body(yaml_file: str, candidates: list[dict], errors: list[str],
                mw_release: str, source_sha: str, previous: str = "",
                today: str | None = None, details_url: str | None = None) -> str:
    today = today or datetime.date.today().isoformat()
    ticks = previous_ticks(previous)
    candidates = _sorted(candidates)

    for max_commits in (10, 8, 6, 5, 4, 3, 2, 1, 0):
        lines = [
            FILE_MARKER.format(yaml_file),
            f"Pinned revisions in `{yaml_file}` with significant changes, scanned {today} "
            f"against {source_sha[:7]}. Only revisions whose `requires.MediaWiki` accepts "
            f"MediaWiki {mw_release} are offered.",
            "",
            "Tick the updates to take, then add the `apply-pin-updates` label to open a "
            "pull request. Ticks are kept when the weekly scan refreshes this issue, "
            "unless the offered revision changes.",
        ]
        if details_url:
            lines += [
                "",
                f"Each entry below lists up to {max_commits} significant commits. For the "
                f"full list of significant commits for every entry, open the "
                f"[summary page of the scan that wrote this issue]({details_url}). That page "
                "is for reading only; make your choices here.",
            ]
        for key, title, default in SECTIONS:
            section = [c for c in candidates if c["category"] == key]
            if not section:
                continue
            lines += ["", f"### {title} ({len(section)})", ""]
            for c in section:
                checked = ticks.get((c["kind"], c["name"], c["new"]), default)
                lines += _entry_lines(c, checked, max_commits, details_url)
        if not candidates:
            lines += ["", "No pins have significant changes."]
        if errors:
            lines += ["", "### Could not scan", ""] + [f"- {e}" for e in errors]
        body = "\n".join(lines) + "\n"
        if len(body) <= MAX_BODY_CHARS:
            return body
    return body


# ── Applying ─────────────────────────────────────────────────────────────────

def ticked(body: str) -> list[dict]:
    selected = []
    for line in body.splitlines():
        m = MARKER_RE.match(line)
        if not m or m.group(1) == " ":
            continue
        data = json.loads(m.group(2))
        if not SHA_RE.match(data.get("commit", "")):
            raise ValueError(f"bad commit for {data.get('name')}: {data.get('commit')!r}")
        selected.append(data)
    return selected


def body_yaml_file(body: str) -> str | None:
    m = re.search(FILE_MARKER.format(r"(\S+)"), body)
    return m.group(1) if m else None


def apply_updates(yaml_text: str, updates: list[dict]) -> tuple[str, list[str]]:
    """Rewrite the commit line of each selected entry; return text and a change log."""
    wanted = {(u["kind"], u["name"]): u for u in updates}
    out, changes = [], []
    kind = name = None
    for line in yaml_text.splitlines(keepends=True):
        if line.rstrip("\n") in ("extensions:", "skins:"):
            kind, name = line.rstrip("\n")[:-2], None
        elif m := re.match(r"^- ([^:]+):\s*$", line):
            name = m.group(1)
        elif (m := re.match(r"^(\s+commit:\s*)([0-9a-f]{40})(\s*#.*)?$", line.rstrip("\n"))) \
                and (kind, name) in wanted:
            u = wanted.pop((kind, name))
            had_comment = bool(m.group(3))
            label = u.get("version") or (u.get("declared") if had_comment else None)
            comment = f" # v. {label}" if label else ""
            changes.append(f"{name}: {m.group(2)[:7]} → {u['commit'][:7]}"
                           + (f" ({label})" if label else ""))
            line = f"{m.group(1)}{u['commit']}{comment}\n"
        out.append(line)
    if wanted:
        missing = ", ".join(name for _, name in wanted)
        raise ValueError(f"no commit line in the YAML file for: {missing}")
    return "".join(out), changes


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="Write the issue body for available updates")
    p_scan.add_argument("yaml_file")
    p_scan.add_argument("--output", required=True)
    p_scan.add_argument("--previous", help="Current issue body, to keep ticks")
    p_scan.add_argument("--cache", help="Clone cache directory (default: a temp dir)")
    p_scan.add_argument("--details-output",
                        help="Also write every significant commit, without checkboxes, here")
    p_scan.add_argument("--details-url",
                        help="Where the details are published; long lists in the issue link here")

    p_apply = sub.add_parser("apply", help="Apply ticked updates from an issue body")
    p_apply.add_argument("yaml_file")
    p_apply.add_argument("--body", required=True)
    p_apply.add_argument("--summary", help="Write a Markdown list of changes here")

    args = parser.parse_args()

    if args.command == "scan":
        cache = args.cache or tempfile.mkdtemp(prefix="pin-updates-")
        candidates, errors, mw_release = scan(args.yaml_file, cache)
        previous = open(args.previous).read() if args.previous else ""
        source = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                                text=True).stdout.strip() or "unknown"
        yaml_file = os.path.basename(args.yaml_file)
        body = render_body(yaml_file, candidates, errors, mw_release, source, previous,
                           details_url=args.details_url)
        with open(args.output, "w") as f:
            f.write(body)
        if args.details_output:
            with open(args.details_output, "w") as f:
                f.write(render_details(yaml_file, candidates, mw_release, source))
        print(f"{len(candidates)} candidates, {len(errors)} errors")
        return 0

    body = open(args.body).read()
    named = body_yaml_file(body)
    if named and named != os.path.basename(args.yaml_file):
        print(f"ERROR: issue is for {named}, not {args.yaml_file}", file=sys.stderr)
        return 1
    updates = ticked(body)
    text, changes = apply_updates(open(args.yaml_file).read(), updates)
    with open(args.yaml_file, "w") as f:
        f.write(text)
    if args.summary:
        with open(args.summary, "w") as f:
            f.write("".join(f"- {c}\n" for c in changes))
    print(f"applied {len(changes)} updates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
