# Automated Version Bump Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a `dev` → `master` pull request raise the version across every versioned file on its own, so merging it publishes a release through the existing `release.yml`.

**Architecture:** One dependency-free Python script, `tools/bump_version.py`, reads `CHANGELOG.md` for release intent and the conventional-commit log for magnitude, rewrites seven versioned files (thirteen when the minor moves), and verifies the result against the same gate `release.yml` applies. One `pull_request` workflow runs it on `dev` → `master` PRs and pushes a single reviewable commit to `dev`. `release.yml` is not modified.

**Tech Stack:** Python 3.11+ standard library only (`re`, `json`, `subprocess`, `dataclasses`, `pathlib`, `datetime`, `argparse`, `tomllib`). GitHub Actions. `unittest` via the repository's existing `unittest discover -s python`.

**Spec:** `docs/superpowers/specs/2026-09-01-automated-version-bump-design.md`

## Global Constraints

- Python 3.11 is the floor. `tomllib` is 3.11+; the repository's own `requires-python` is `>=3.11`. The local default `python3` on the developer's machine is 3.9 and **cannot** run these tests — use `python3.14`.
- No third-party Python dependencies. The bump job installs nothing.
- `.github/workflows/release.yml` must not be edited. Its path and the `release` environment name are registered with npm, PyPI, and crates.io as OIDC Trusted Publisher config.
- While the major version is 0, no input produces a major bump. `BREAKING CHANGE` and `!` map to minor.
- Repository URL for changelog links: `https://github.com/GagaKor/rtsp-backchannel`.
- Tags are `v` + version, e.g. `v0.3.1`.
- Commit messages: Conventional Commits, body in English, ending with the trailer `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.
- Tests live in `python/test_*.py` and run as `PYTHONPATH=python:. python3.14 -m unittest discover -s python -p 'test_*.py'` from the repository root. `tools` is importable from there as a namespace package (verified).

---

## File Structure

| File | Responsibility |
|---|---|
| `tools/bump_version.py` (create) | The whole bump engine: version arithmetic, commit-level inference, changelog transformation, file rewriting, verification, CLI. |
| `python/test_release_bump.py` (create) | Tests for all of the above. Picked up by the existing `discover -s python`. |
| `.github/workflows/version-bump.yml` (create) | Runs the engine on `dev` → `master` PRs and pushes the bump commit. |
| `RELEASING.md` (modify) | Rewrite the automated-release section around the new flow; demote manual version editing to fallback. |
| `CONTRIBUTING.md` (modify) | Document that commit types set the version and `[Unreleased]` decides whether a release happens. |
| `docs/decisions/2026-09-01-automate-version-bump-on-release-prs.md` (create) | ADR. |

`tools/bump_version.py` stays a single file. It is one cohesive responsibility — "turn a released state plus new work into the next released state" — and every part of it is exercised through the same three entry points (`apply_bump`, `verify`, `main`). Splitting it would put a package boundary inside one algorithm.

### One deliberate simplification of the spec

The spec's table marks the six README install pins "minor bumps only". The implementation instead **always** runs the pin rewriter, replacing the pin derived from the current manifest version with the pin derived from the target. When the minor does not move, old and new pins are identical and the files are left byte-identical — the spec's required behaviour — but the rewriter's exactly-one-match assertion still runs, so a missing or malformed pin is caught on every bump instead of only on minor bumps. This removes a branch and strengthens gate check 4. The spec's test "a patch bump leaves all six READMEs untouched" is kept and still passes.

---

## Task 1: Version arithmetic and commit-level inference

**Files:**
- Create: `tools/bump_version.py`
- Create (test): `python/test_release_bump.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class BumpError(RuntimeError)`
  - `Version` — frozen ordered dataclass with `major: int`, `minor: int`, `patch: int`; `Version.parse(text: str) -> Version`; `__str__() -> str`; `bumped(level: str) -> Version` where `level` is `"major" | "minor" | "patch"`.
  - `level_from_commits(messages: Sequence[str], *, major: int) -> str` returning `"major" | "minor" | "patch"`.

- [ ] **Step 1: Write the failing tests**

Create `python/test_release_bump.py`:

```python
"""Tests for tools/bump_version.py."""

import unittest

from tools.bump_version import BumpError, Version, level_from_commits


class VersionArithmetic(unittest.TestCase):
    def test_parses_and_renders_round_trip(self):
        self.assertEqual(str(Version.parse("0.3.1")), "0.3.1")
        self.assertEqual(Version.parse("0.3.1"), Version(0, 3, 1))

    def test_rejects_non_semver(self):
        for text in ["0.3", "v0.3.1", "0.3.1-rc1", "", "a.b.c"]:
            with self.assertRaises(BumpError):
                Version.parse(text)

    def test_orders_numerically_not_lexically(self):
        self.assertLess(Version.parse("0.9.0"), Version.parse("0.10.0"))
        self.assertLess(Version.parse("0.3.1"), Version.parse("0.4.0"))

    def test_bump_resets_lower_components(self):
        base = Version.parse("0.3.1")
        self.assertEqual(str(base.bumped("patch")), "0.3.2")
        self.assertEqual(str(base.bumped("minor")), "0.4.0")
        self.assertEqual(str(base.bumped("major")), "1.0.0")

    def test_bump_rejects_unknown_level(self):
        with self.assertRaises(BumpError):
            Version.parse("0.3.1").bumped("epic")

    def test_line_is_the_major_minor_pin_prefix(self):
        self.assertEqual(Version.parse("0.3.1").line, "0.3")
        self.assertEqual(Version.parse("0.4.0").line, "0.4")
        self.assertEqual(Version.parse("0.10.2").line, "0.10")


class CommitLevelInference(unittest.TestCase):
    def test_feature_yields_minor(self):
        messages = ["fix(vigi): correct a thing", "feat(cli): add --transport"]
        self.assertEqual(level_from_commits(messages, major=0), "minor")

    def test_fixes_only_yield_patch(self):
        messages = ["fix(vigi): correct a thing", "docs: reword the readme"]
        self.assertEqual(level_from_commits(messages, major=0), "patch")

    def test_no_commits_yields_patch(self):
        self.assertEqual(level_from_commits([], major=0), "patch")

    def test_bang_marks_breaking(self):
        self.assertEqual(level_from_commits(["feat(api)!: drop the old shape"], major=0), "minor")
        self.assertEqual(level_from_commits(["fix!: change a default"], major=0), "minor")

    def test_breaking_change_footer_marks_breaking(self):
        message = "refactor: move pacing\n\nBREAKING CHANGE: SAMPLE_RATE moved modules."
        self.assertEqual(level_from_commits([message], major=0), "minor")

    def test_breaking_change_hyphen_spelling_is_accepted(self):
        message = "refactor: move pacing\n\nBREAKING-CHANGE: SAMPLE_RATE moved modules."
        self.assertEqual(level_from_commits([message], major=0), "minor")

    def test_breaking_never_reaches_major_while_zero(self):
        self.assertEqual(level_from_commits(["feat!: rewrite"], major=0), "minor")

    def test_breaking_reaches_major_once_stable(self):
        self.assertEqual(level_from_commits(["feat!: rewrite"], major=1), "major")

    def test_breaking_words_in_prose_do_not_count(self):
        message = "docs: explain that a breaking change would need a major bump"
        self.assertEqual(level_from_commits([message], major=0), "patch")

    def test_scope_with_punctuation_is_parsed(self):
        self.assertEqual(level_from_commits(["feat(vigi,ptz): add both"], major=0), "minor")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=python:. python3.14 -m unittest test_release_bump -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools.bump_version'`

- [ ] **Step 3: Write the minimal implementation**

Create `tools/bump_version.py`:

```python
#!/usr/bin/env python3
"""Compute and apply the next release version across every versioned file.

Run from the repository root:

    python tools/bump_version.py --root .

Prints a one-line JSON summary on stdout. Exits 0 whether or not a bump
happened; exits non-zero only when a file it must edit is not in the shape it
expects, which is a condition a human has to look at.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

GITHUB_REPO_URL = "https://github.com/GagaKor/rtsp-backchannel"

_VERSION_RE = re.compile(r"\A(\d+)\.(\d+)\.(\d+)\Z")

# A Conventional Commit subject: "type(optional scope)!: description". The "!"
# and a "BREAKING CHANGE:" footer are the two ways the spec recognises a
# breaking change.
_SUBJECT_RE = re.compile(r"\A(?P<type>[a-z]+)(?:\((?P<scope>[^)]*)\))?(?P<bang>!)?: ")
_BREAKING_FOOTER_RE = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)


class BumpError(RuntimeError):
    """A file was not in the shape the bump requires, or an input was invalid."""


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, text: str) -> "Version":
        match = _VERSION_RE.match(text.strip())
        if match is None:
            raise BumpError(f"not a MAJOR.MINOR.PATCH version: {text!r}")
        return cls(int(match[1]), int(match[2]), int(match[3]))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    @property
    def line(self) -> str:
        """The MAJOR.MINOR prefix the README install pins carry."""
        return f"{self.major}.{self.minor}"

    def bumped(self, level: str) -> "Version":
        if level == "major":
            return Version(self.major + 1, 0, 0)
        if level == "minor":
            return Version(self.major, self.minor + 1, 0)
        if level == "patch":
            return Version(self.major, self.minor, self.patch + 1)
        raise BumpError(f"unknown bump level: {level!r}")


def _is_breaking(message: str) -> bool:
    subject = message.splitlines()[0] if message.splitlines() else ""
    match = _SUBJECT_RE.match(subject)
    if match is not None and match["bang"]:
        return True
    return _BREAKING_FOOTER_RE.search(message) is not None


def _is_feature(message: str) -> bool:
    subject = message.splitlines()[0] if message.splitlines() else ""
    match = _SUBJECT_RE.match(subject)
    return match is not None and match["type"] == "feat"


def level_from_commits(messages: Sequence[str], *, major: int) -> str:
    """Map conventional-commit types onto a bump level.

    While the major version is 0 a breaking change moves the minor, not the
    major: at 0.x the minor position is the compatibility boundary, and the
    install pins already stop at the next minor. Promoting to 1.0.0 is a claim
    only a human makes.
    """
    if any(_is_breaking(message) for message in messages):
        return "major" if major > 0 else "minor"
    if any(_is_feature(message) for message in messages):
        return "minor"
    return "patch"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=python:. python3.14 -m unittest test_release_bump -v`
Expected: PASS, 16 tests.

- [ ] **Step 5: Commit**

```bash
git add tools/bump_version.py python/test_release_bump.py
git commit -F - <<'EOF'
feat(release): infer a bump level from conventional commits

The publish pipeline compares each manifest version against what is live on
npm, PyPI and crates.io, but nothing computes the next version, so a merge with
no hand-written bump ships nothing. This is the arithmetic half of closing that
gap.

Breaking changes map to minor rather than major while the major version is 0.
At 0.x the minor position is the compatibility boundary and the install pins
(^0.3, >=0.3,<0.4, "0.3") already stop at the next minor, so a consumer pinned
to 0.3 does not silently receive a breaking 0.4. The major branch is
implemented and tested anyway so that the policy is explicit rather than an
accident of the repository's current version.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
```

---

## Task 2: Changelog parse and render round-trip

**Files:**
- Modify: `tools/bump_version.py`
- Modify (test): `python/test_release_bump.py`

**Interfaces:**
- Consumes: `BumpError`, `Version` from Task 1.
- Produces:
  - `Section` — frozen dataclass with `version: str` (`"Unreleased"` or `"0.3.1"`), `date: str | None`, `body: tuple[str, ...]`.
  - `Changelog` — frozen dataclass with `head: tuple[str, ...]`, `sections: tuple[Section, ...]`, `links: tuple[str, ...]`; `Changelog.parse(text: str) -> Changelog`; `render() -> str`.

Parse and render must be exact inverses on the real `CHANGELOG.md`. Everything in Task 3 rewrites a parsed tree and re-renders it, so a lossy round-trip would silently reformat the whole file on every release.

- [ ] **Step 1: Write the failing tests**

Append to `python/test_release_bump.py` (and extend the import line at the top to `from tools.bump_version import BumpError, Changelog, Version, level_from_commits`):

```python
import pathlib

CHANGELOG_FIXTURE = """\
# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- A second audio-send transport.

## [0.3.1] - 2026-08-13

### Changed

- Corrected the stated Node minimum.

## [0.3.0] - 2026-08-11

### Added

- PTZ movement control.

[Unreleased]: https://github.com/GagaKor/rtsp-backchannel/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/GagaKor/rtsp-backchannel/releases/tag/v0.3.1
[0.3.0]: https://github.com/GagaKor/rtsp-backchannel/releases/tag/v0.3.0
"""


class ChangelogRoundTrip(unittest.TestCase):
    def test_round_trips_the_fixture_byte_for_byte(self):
        self.assertEqual(Changelog.parse(CHANGELOG_FIXTURE).render(), CHANGELOG_FIXTURE)

    def test_round_trips_the_real_changelog_byte_for_byte(self):
        text = pathlib.Path("CHANGELOG.md").read_text(encoding="utf-8")
        self.assertEqual(Changelog.parse(text).render(), text)

    def test_separates_head_sections_and_links(self):
        changelog = Changelog.parse(CHANGELOG_FIXTURE)

        self.assertEqual(changelog.head[0], "# Changelog")
        self.assertEqual(
            [section.version for section in changelog.sections],
            ["Unreleased", "0.3.1", "0.3.0"],
        )
        self.assertEqual(changelog.sections[1].date, "2026-08-13")
        self.assertIsNone(changelog.sections[0].date)
        self.assertEqual(len(changelog.links), 3)
        self.assertTrue(changelog.links[0].startswith("[Unreleased]: "))

    def test_body_excludes_surrounding_blank_lines(self):
        changelog = Changelog.parse(CHANGELOG_FIXTURE)

        self.assertEqual(
            changelog.sections[0].body,
            ("### Added", "", "- A second audio-send transport."),
        )

    def test_empty_unreleased_round_trips(self):
        text = CHANGELOG_FIXTURE.replace(
            "## [Unreleased]\n\n### Added\n\n- A second audio-send transport.\n\n",
            "## [Unreleased]\n\n",
        )

        parsed = Changelog.parse(text)

        self.assertEqual(parsed.sections[0].body, ())
        self.assertEqual(parsed.render(), text)

    def test_rejects_a_changelog_with_no_sections(self):
        with self.assertRaises(BumpError):
            Changelog.parse("# Changelog\n\nNothing here.\n")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=python:. python3.14 -m unittest test_release_bump -v`
Expected: FAIL — `ImportError: cannot import name 'Changelog'`

- [ ] **Step 3: Write the minimal implementation**

Add to `tools/bump_version.py` (after `level_from_commits`):

```python
_SECTION_RE = re.compile(r"\A## \[(?P<version>[^\]]+)\](?: - (?P<date>\d{4}-\d{2}-\d{2}))?\s*\Z")
_LINK_RE = re.compile(r"\A\[[^\]]+\]: \S+\s*\Z")


def _trimmed(lines: Sequence[str]) -> tuple[str, ...]:
    """Drop leading and trailing blank lines."""
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return tuple(lines[start:end])


@dataclass(frozen=True)
class Section:
    version: str
    date: str | None
    body: tuple[str, ...]

    @property
    def heading(self) -> str:
        if self.date is None:
            return f"## [{self.version}]"
        return f"## [{self.version}] - {self.date}"


@dataclass(frozen=True)
class Changelog:
    head: tuple[str, ...]
    sections: tuple[Section, ...]
    links: tuple[str, ...]

    @classmethod
    def parse(cls, text: str) -> "Changelog":
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines = lines[:-1]

        starts = [i for i, line in enumerate(lines) if _SECTION_RE.match(line)]
        if not starts:
            raise BumpError("CHANGELOG.md has no '## [version]' sections")

        head = _trimmed(lines[: starts[0]])

        sections: list[Section] = []
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else len(lines)
            match = _SECTION_RE.match(lines[start])
            assert match is not None  # starts was built from the same predicate
            sections.append(
                Section(
                    version=match["version"],
                    date=match["date"],
                    body=_trimmed(lines[start + 1 : end]),
                )
            )

        # The link reference block is the trailing run of "[name]: url" lines in
        # the final section's body.
        last = sections[-1]
        body = list(last.body)
        links: list[str] = []
        while body and _LINK_RE.match(body[-1]):
            links.insert(0, body.pop())
        sections[-1] = Section(last.version, last.date, _trimmed(body))

        return cls(head=head, sections=tuple(sections), links=tuple(links))

    def render(self) -> str:
        out: list[str] = [*self.head, ""]
        for section in self.sections:
            out.append(section.heading)
            out.append("")
            if section.body:
                out.extend(section.body)
                out.append("")
        out.extend(self.links)
        return "\n".join(out) + "\n"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=python:. python3.14 -m unittest test_release_bump -v`
Expected: PASS, 22 tests. `test_round_trips_the_real_changelog_byte_for_byte` is the one that matters — it proves the parser handles the real file, including the two historical sections (`0.2.0`, `0.1.0`) that have no link reference.

- [ ] **Step 5: Commit**

```bash
git add tools/bump_version.py python/test_release_bump.py
git commit -F - <<'EOF'
feat(release): parse and render CHANGELOG.md losslessly

Promotion rewrites a parsed tree and re-renders it, so a lossy round-trip
would reformat the entire changelog on every release and bury the actual
release diff. Parse and render are therefore held to byte-for-byte equality
against the real CHANGELOG.md, not just against a fixture -- the real file
carries cases a fixture would not think to include, such as the 0.2.0 and
0.1.0 sections that have no link reference.

The link block is recovered as the trailing run of reference lines in the last
section's body rather than by position, because the file ends with whichever
section happens to be oldest.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
```

---

## Task 3: Changelog promotion, including the re-run fold

**Files:**
- Modify: `tools/bump_version.py`
- Modify (test): `python/test_release_bump.py`

**Interfaces:**
- Consumes: `Changelog`, `Section`, `Version`, `BumpError`.
- Produces, as methods on `Changelog`:
  - `pending() -> tuple[str, ...]` — the body currently under `## [Unreleased]`.
  - `staged(last_released: Version) -> Section | None` — the dated section, if any, whose version is greater than `last_released`; this is a previous run's output.
  - `promoted(target: Version, last_released: Version, today: str) -> Changelog` — `today` is an ISO `YYYY-MM-DD` string.

This is the task the spec calls the main correctness risk of bumping on the PR. `promoted` does not patch the previous run's output incrementally; it recomputes the end state.

- [ ] **Step 1: Write the failing tests**

Append to `python/test_release_bump.py`:

```python
STAGED_FIXTURE = """\
# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- A CLI flag that arrived after the PR opened.

## [0.3.2] - 2026-08-30

### Fixed

- A thing the first run already promoted.

## [0.3.1] - 2026-08-13

### Changed

- Corrected the stated Node minimum.

[Unreleased]: https://github.com/GagaKor/rtsp-backchannel/compare/v0.3.2...HEAD
[0.3.2]: https://github.com/GagaKor/rtsp-backchannel/releases/tag/v0.3.2
[0.3.1]: https://github.com/GagaKor/rtsp-backchannel/releases/tag/v0.3.1
"""


class ChangelogPromotion(unittest.TestCase):
    def test_pending_returns_the_unreleased_body(self):
        self.assertEqual(
            Changelog.parse(CHANGELOG_FIXTURE).pending(),
            ("### Added", "", "- A second audio-send transport."),
        )

    def test_no_staged_section_on_a_freshly_released_tree(self):
        changelog = Changelog.parse(CHANGELOG_FIXTURE)
        self.assertIsNone(changelog.staged(Version.parse("0.3.1")))

    def test_staged_section_is_the_one_ahead_of_the_last_release(self):
        staged = Changelog.parse(STAGED_FIXTURE).staged(Version.parse("0.3.1"))

        self.assertIsNotNone(staged)
        self.assertEqual(staged.version, "0.3.2")

    def test_fresh_promotion_dates_the_unreleased_body(self):
        promoted = Changelog.parse(CHANGELOG_FIXTURE).promoted(
            target=Version.parse("0.4.0"),
            last_released=Version.parse("0.3.1"),
            today="2026-09-01",
        )

        self.assertEqual(
            [section.version for section in promoted.sections],
            ["Unreleased", "0.4.0", "0.3.1", "0.3.0"],
        )
        self.assertEqual(promoted.sections[0].body, ())
        self.assertEqual(promoted.sections[1].date, "2026-09-01")
        self.assertEqual(
            promoted.sections[1].body,
            ("### Added", "", "- A second audio-send transport."),
        )

    def test_fresh_promotion_rewrites_the_link_references(self):
        promoted = Changelog.parse(CHANGELOG_FIXTURE).promoted(
            target=Version.parse("0.4.0"),
            last_released=Version.parse("0.3.1"),
            today="2026-09-01",
        )

        self.assertEqual(
            promoted.links,
            (
                "[Unreleased]: https://github.com/GagaKor/rtsp-backchannel/compare/v0.4.0...HEAD",
                "[0.4.0]: https://github.com/GagaKor/rtsp-backchannel/releases/tag/v0.4.0",
                "[0.3.1]: https://github.com/GagaKor/rtsp-backchannel/releases/tag/v0.3.1",
                "[0.3.0]: https://github.com/GagaKor/rtsp-backchannel/releases/tag/v0.3.0",
            ),
        )

    def test_rerun_folds_new_prose_into_the_staged_section_and_raises_it(self):
        promoted = Changelog.parse(STAGED_FIXTURE).promoted(
            target=Version.parse("0.4.0"),
            last_released=Version.parse("0.3.1"),
            today="2026-09-02",
        )

        self.assertEqual(
            [section.version for section in promoted.sections],
            ["Unreleased", "0.4.0", "0.3.1"],
        )
        self.assertEqual(promoted.sections[1].date, "2026-09-02")
        self.assertEqual(
            promoted.sections[1].body,
            (
                "### Fixed",
                "",
                "- A thing the first run already promoted.",
                "",
                "### Added",
                "",
                "- A CLI flag that arrived after the PR opened.",
            ),
        )

    def test_rerun_does_not_accumulate_a_stale_link_reference(self):
        promoted = Changelog.parse(STAGED_FIXTURE).promoted(
            target=Version.parse("0.4.0"),
            last_released=Version.parse("0.3.1"),
            today="2026-09-02",
        )

        self.assertNotIn(
            "[0.3.2]: https://github.com/GagaKor/rtsp-backchannel/releases/tag/v0.3.2",
            promoted.links,
        )
        self.assertEqual(promoted.links[0].split(": ")[1].split("/compare/")[1], "v0.4.0...HEAD")

    def test_promotion_output_survives_a_render_parse_round_trip(self):
        promoted = Changelog.parse(STAGED_FIXTURE).promoted(
            target=Version.parse("0.4.0"),
            last_released=Version.parse("0.3.1"),
            today="2026-09-02",
        )
        text = promoted.render()

        self.assertEqual(Changelog.parse(text).render(), text)

    def test_exactly_one_dated_section_for_the_target(self):
        text = Changelog.parse(STAGED_FIXTURE).promoted(
            target=Version.parse("0.4.0"),
            last_released=Version.parse("0.3.1"),
            today="2026-09-02",
        ).render()

        self.assertEqual(text.count("## [0.4.0] - "), 1)

    def test_missing_unreleased_section_is_an_error(self):
        text = CHANGELOG_FIXTURE.replace("## [Unreleased]\n\n### Added\n\n- A second audio-send transport.\n\n", "")
        with self.assertRaises(BumpError):
            Changelog.parse(text).pending()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=python:. python3.14 -m unittest test_release_bump -v`
Expected: FAIL — `AttributeError: 'Changelog' object has no attribute 'pending'`

- [ ] **Step 3: Write the minimal implementation**

Add these methods to `Changelog` in `tools/bump_version.py`:

```python
    def _unreleased(self) -> Section:
        for section in self.sections:
            if section.version == "Unreleased":
                return section
        raise BumpError("CHANGELOG.md has no '## [Unreleased]' section")

    def pending(self) -> tuple[str, ...]:
        return self._unreleased().body

    def staged(self, last_released: Version) -> Section | None:
        """The dated section a previous bump run already created, if any."""
        for section in self.sections:
            if section.version == "Unreleased" or section.date is None:
                continue
            try:
                version = Version.parse(section.version)
            except BumpError:
                continue
            if version > last_released:
                return section
        return None

    def promoted(self, target: Version, last_released: Version, today: str) -> "Changelog":
        """Recompute the whole end state rather than patching a previous run.

        A pull request that gains commits after the first bump needs the staged
        section's heading raised and the newly written [Unreleased] prose folded
        into it. Recomputing from the last release makes that one code path
        instead of a special case.
        """
        pending = self.pending()
        staged = self.staged(last_released)

        body: tuple[str, ...] = staged.body if staged is not None else ()
        if body and pending:
            body = body + ("",) + pending
        elif pending:
            body = pending

        carried = tuple(
            section
            for section in self.sections
            if section.version != "Unreleased" and section is not staged
        )
        sections = (
            Section("Unreleased", None, ()),
            Section(str(target), today, body),
            *carried,
        )

        dropped = {"[Unreleased]:", f"[{target}]:"}
        if staged is not None:
            dropped.add(f"[{staged.version}]:")
        links = (
            f"[Unreleased]: {GITHUB_REPO_URL}/compare/v{target}...HEAD",
            f"[{target}]: {GITHUB_REPO_URL}/releases/tag/v{target}",
            *(link for link in self.links if link.split(" ", 1)[0] not in dropped),
        )

        return Changelog(head=self.head, sections=sections, links=links)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=python:. python3.14 -m unittest test_release_bump -v`
Expected: PASS, 32 tests.

- [ ] **Step 5: Commit**

```bash
git add tools/bump_version.py python/test_release_bump.py
git commit -F - <<'EOF'
feat(release): promote the Unreleased section into a dated release

The hardest case is not the first run but the second. A pull request that
gains commits after its bump commit has both a dated section the first run
created and fresh [Unreleased] prose, and a feat among the new commits raises
the level, so the staged heading is wrong too.

promoted() recomputes the entire end state from the last release instead of
patching the previous run's output. Folding the staged body and the pending
body, renaming the heading and replacing the stale link reference then fall
out of one code path rather than being three special cases, and the first run
is just the case where nothing is staged.

Emptying [Unreleased] doubles as the idempotency latch: a re-run with no new
work finds nothing pending and stops before touching a file.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
```

---

## Task 4: Rewriting the manifests, the pins, and the version assertion

**Files:**
- Modify: `tools/bump_version.py`
- Modify (test): `python/test_release_bump.py`

**Interfaces:**
- Consumes: `Version`, `BumpError`.
- Produces:
  - `rewrite_manifests(root: Path, old: Version, new: Version) -> None`
  - `rewrite_readme_pins(root: Path, old: Version, new: Version) -> None`
  - `rewrite_version_assertion(root: Path, old: Version, new: Version) -> None`
  - `read_manifest_version(root: Path) -> Version` — the version in `package.json`.
  - `verify(root: Path, target: Version) -> None` — gate checks 1, 2 and 4; raises `BumpError`.
  - Test helper (in the test file, not the module): `write_fixture_repo(root: Path, *, version: str, unreleased: str) -> None`.

Every substitution asserts it matched exactly once. A silent zero-match is how a manifest gets left behind, and a manifest left behind fails `release.yml` at its gate with three registries already half-considered.

- [ ] **Step 1: Write the failing tests**

Append to `python/test_release_bump.py` (extend the import line to add `GITHUB_REPO_URL, read_manifest_version, rewrite_manifests, rewrite_readme_pins, rewrite_version_assertion, verify`, and add `import tempfile` at the top):

```python
def write_fixture_repo(root: pathlib.Path, *, version: str = "0.3.1", unreleased: str = "") -> None:
    """Write the eleven versioned files in the shapes the real repository uses."""
    line = ".".join(version.split(".")[:2])
    next_minor = f"{line.split('.')[0]}.{int(line.split('.')[1]) + 1}"

    (root / "package.json").write_text(
        '{\n  "name": "rtsp-backchannel",\n'
        f'  "version": "{version}",\n'
        '  "type": "module"\n}\n',
        encoding="utf-8",
    )
    (root / "package-lock.json").write_text(
        '{\n  "name": "rtsp-backchannel",\n'
        f'  "version": "{version}",\n'
        '  "lockfileVersion": 3,\n  "requires": true,\n'
        '  "packages": {\n    "": {\n      "name": "rtsp-backchannel",\n'
        f'      "version": "{version}",\n'
        '      "license": "MIT OR Apache-2.0"\n    },\n'
        '    "node_modules/@types/node": {\n      "version": "26.2.0"\n    }\n  }\n}\n',
        encoding="utf-8",
    )
    (root / "python").mkdir(exist_ok=True)
    (root / "rust").mkdir(exist_ok=True)
    (root / "python" / "pyproject.toml").write_text(
        '[project]\nname = "rtsp-backchannel"\n'
        f'version = "{version}"\nrequires-python = ">=3.11"\n',
        encoding="utf-8",
    )
    (root / "rust" / "Cargo.toml").write_text(
        '[package]\nname = "rtsp-backchannel"\n'
        f'version = "{version}"\nedition = "2024"\nrust-version = "1.86"\n\n'
        '[dependencies]\nclap = { version = "4.5", features = ["derive"] }\n',
        encoding="utf-8",
    )
    (root / "rust" / "Cargo.lock").write_text(
        'version = 4\n\n[[package]]\nname = "anyhow"\nversion = "1.0.100"\n\n'
        '[[package]]\nname = "rtsp-backchannel"\n'
        f'version = "{version}"\ndependencies = [\n "anyhow",\n]\n',
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        f"# rtsp-backchannel\n\n```bash\nnpm install rtsp-backchannel@^{line}\n```\n",
        encoding="utf-8",
    )
    (root / "README.ko.md").write_text(
        f"# rtsp-backchannel\n\n```bash\nnpm install rtsp-backchannel@^{line}\n```\n",
        encoding="utf-8",
    )
    for name in ["README.md", "README.ko.md"]:
        (root / "python" / name).write_text(
            f"# rtsp-backchannel\n\n```bash\npython3 -m pip install "
            f"'rtsp-backchannel>={line},<{next_minor}'\n```\n",
            encoding="utf-8",
        )
        (root / "rust" / name).write_text(
            f'# rtsp-backchannel\n\n```toml\n[dependencies]\n'
            f'rtsp-backchannel = "{line}"\n```\n',
            encoding="utf-8",
        )
    (root / "python" / "test_library_api.py").write_text(
        "import unittest\n\n\nclass T(unittest.TestCase):\n"
        "    def test_declares_installable_wheel_metadata(self):\n"
        f'        self.assertEqual(metadata["project"]["version"], "{version}")\n',
        encoding="utf-8",
    )
    body = f"\n{unreleased}\n" if unreleased else "\n"
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\nAll notable changes.\n\n"
        f"## [Unreleased]\n{body}\n"
        f"## [{version}] - 2026-08-13\n\n### Changed\n\n- Something.\n\n"
        f"[Unreleased]: {GITHUB_REPO_URL}/compare/v{version}...HEAD\n"
        f"[{version}]: {GITHUB_REPO_URL}/releases/tag/v{version}\n",
        encoding="utf-8",
    )


class FixtureRepoShape(unittest.TestCase):
    def test_fixture_writes_every_versioned_file(self):
        """Guards the helper: a site added to the module needs one here too.

        This proves the fixture is complete, NOT that the rewriters match the
        real repository. Task 4 Step 5 is what proves that.
        """
        with tempfile.TemporaryDirectory() as name:
            root = pathlib.Path(name)
            write_fixture_repo(root)

            for relative in [
                "package.json",
                "package-lock.json",
                "python/pyproject.toml",
                "rust/Cargo.toml",
                "rust/Cargo.lock",
                "README.md",
                "README.ko.md",
                "python/README.md",
                "python/README.ko.md",
                "rust/README.md",
                "rust/README.ko.md",
                "python/test_library_api.py",
                "CHANGELOG.md",
            ]:
                self.assertTrue((root / relative).is_file(), relative)


class ManifestRewriting(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        write_fixture_repo(self.root)

    def test_reads_the_manifest_version(self):
        self.assertEqual(read_manifest_version(self.root), Version.parse("0.3.1"))

    def test_rewrites_all_five_manifest_values(self):
        rewrite_manifests(self.root, Version.parse("0.3.1"), Version.parse("0.4.0"))

        self.assertIn('"version": "0.4.0"', (self.root / "package.json").read_text())
        lock = (self.root / "package-lock.json").read_text()
        self.assertEqual(lock.count('"version": "0.4.0"'), 2)
        self.assertIn('"version": "26.2.0"', lock)
        self.assertIn('version = "0.4.0"', (self.root / "python" / "pyproject.toml").read_text())
        self.assertIn('version = "0.4.0"', (self.root / "rust" / "Cargo.toml").read_text())
        self.assertIn(
            'name = "rtsp-backchannel"\nversion = "0.4.0"',
            (self.root / "rust" / "Cargo.lock").read_text(),
        )

    def test_leaves_dependency_versions_alone(self):
        rewrite_manifests(self.root, Version.parse("0.3.1"), Version.parse("0.4.0"))

        self.assertIn('version = "4.5"', (self.root / "rust" / "Cargo.toml").read_text())
        self.assertIn('rust-version = "1.86"', (self.root / "rust" / "Cargo.toml").read_text())
        self.assertIn(
            'name = "anyhow"\nversion = "1.0.100"',
            (self.root / "rust" / "Cargo.lock").read_text(),
        )

    def test_a_manifest_out_of_sync_is_an_error(self):
        (self.root / "rust" / "Cargo.toml").write_text(
            '[package]\nname = "rtsp-backchannel"\nversion = "0.2.9"\n', encoding="utf-8"
        )

        with self.assertRaises(BumpError) as caught:
            rewrite_manifests(self.root, Version.parse("0.3.1"), Version.parse("0.4.0"))

        self.assertIn("Cargo.toml", str(caught.exception))

    def test_minor_bump_rewrites_all_six_readme_pins(self):
        rewrite_readme_pins(self.root, Version.parse("0.3.1"), Version.parse("0.4.0"))

        self.assertIn("rtsp-backchannel@^0.4", (self.root / "README.md").read_text())
        self.assertIn("rtsp-backchannel@^0.4", (self.root / "README.ko.md").read_text())
        self.assertIn("'rtsp-backchannel>=0.4,<0.5'", (self.root / "python" / "README.md").read_text())
        self.assertIn("'rtsp-backchannel>=0.4,<0.5'", (self.root / "python" / "README.ko.md").read_text())
        self.assertIn('rtsp-backchannel = "0.4"', (self.root / "rust" / "README.md").read_text())
        self.assertIn('rtsp-backchannel = "0.4"', (self.root / "rust" / "README.ko.md").read_text())

    def test_patch_bump_leaves_all_six_readmes_byte_identical(self):
        before = {
            name: (self.root / name).read_bytes()
            for name in [
                "README.md",
                "README.ko.md",
                "python/README.md",
                "python/README.ko.md",
                "rust/README.md",
                "rust/README.ko.md",
            ]
        }

        rewrite_readme_pins(self.root, Version.parse("0.3.1"), Version.parse("0.3.2"))

        for name, content in before.items():
            self.assertEqual((self.root / name).read_bytes(), content, name)

    def test_a_missing_pin_is_an_error(self):
        (self.root / "rust" / "README.md").write_text("# no pin here\n", encoding="utf-8")

        with self.assertRaises(BumpError) as caught:
            rewrite_readme_pins(self.root, Version.parse("0.3.1"), Version.parse("0.4.0"))

        self.assertIn("README.md", str(caught.exception))

    def test_rewrites_the_python_version_assertion(self):
        rewrite_version_assertion(self.root, Version.parse("0.3.1"), Version.parse("0.4.0"))

        self.assertIn(
            'self.assertEqual(metadata["project"]["version"], "0.4.0")',
            (self.root / "python" / "test_library_api.py").read_text(),
        )


class GateVerification(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        write_fixture_repo(self.root)

    def test_a_consistent_tree_verifies(self):
        verify(self.root, Version.parse("0.3.1"))

    def test_a_mismatched_manifest_fails_verification(self):
        (self.root / "python" / "pyproject.toml").write_text(
            '[project]\nname = "rtsp-backchannel"\nversion = "0.2.9"\n', encoding="utf-8"
        )

        with self.assertRaises(BumpError) as caught:
            verify(self.root, Version.parse("0.3.1"))

        self.assertIn("pyproject.toml", str(caught.exception))

    def test_a_missing_dated_section_fails_verification(self):
        with self.assertRaises(BumpError) as caught:
            verify(self.root, Version.parse("0.4.0"))

        self.assertIn("CHANGELOG.md", str(caught.exception))

    def test_a_duplicated_dated_section_fails_verification(self):
        path = self.root / "CHANGELOG.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "## [0.3.1] - 2026-08-13",
                "## [0.3.1] - 2026-08-13\n\n### Added\n\n- Dup.\n\n## [0.3.1] - 2026-08-14",
                1,
            ),
            encoding="utf-8",
        )

        with self.assertRaises(BumpError):
            verify(self.root, Version.parse("0.3.1"))

    def test_a_stale_readme_pin_fails_verification(self):
        (self.root / "rust" / "README.md").write_text(
            '# rtsp-backchannel\n\n```toml\n[dependencies]\nrtsp-backchannel = "0.2"\n```\n',
            encoding="utf-8",
        )

        with self.assertRaises(BumpError) as caught:
            verify(self.root, Version.parse("0.3.1"))

        self.assertIn("README.md", str(caught.exception))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=python:. python3.14 -m unittest test_release_bump -v`
Expected: FAIL — `ImportError: cannot import name 'rewrite_manifests'`

- [ ] **Step 3: Write the minimal implementation**

Add `import json` and `from pathlib import Path` to the imports in `tools/bump_version.py`, then append.

Every one of the eleven edits replaces a **fixed string** whose old and new forms are both fully determined by the two versions. So none of this uses regular expressions: each site is described once as a template, and the same template produces both the needle to find and the text to write. That keeps the needle and the replacement from drifting apart, and removes group-numbering and backtracking from the part of the system that must never silently match the wrong thing.

```python
# Each site is one template. Formatting it with the old version gives the exact
# text to find; formatting it with the new version gives the exact text to
# write. Deriving both from one string is what makes them impossible to
# misalign.
_MANIFEST_SITES: list[tuple[str, str]] = [
    ("package.json", '\n  "version": "{version}",\n'),
    # Anchored on surrounding structure rather than on the version alone: a
    # dependency could legitimately share the project's version number.
    (
        "package-lock.json",
        '{{\n  "name": "rtsp-backchannel",\n  "version": "{version}",\n',
    ),
    (
        "package-lock.json",
        '    "": {{\n      "name": "rtsp-backchannel",\n      "version": "{version}",\n',
    ),
    ("python/pyproject.toml", '\nversion = "{version}"\n'),
    ("rust/Cargo.toml", '\nversion = "{version}"\n'),
    ("rust/Cargo.lock", '\nname = "rtsp-backchannel"\nversion = "{version}"\n'),
]

_PIN_SITES: list[tuple[str, str]] = [
    ("README.md", "npm install rtsp-backchannel@^{line}\n"),
    ("README.ko.md", "npm install rtsp-backchannel@^{line}\n"),
    ("python/README.md", "'rtsp-backchannel>={line},<{following}'"),
    ("python/README.ko.md", "'rtsp-backchannel>={line},<{following}'"),
    ("rust/README.md", 'rtsp-backchannel = "{line}"\n'),
    ("rust/README.ko.md", 'rtsp-backchannel = "{line}"\n'),
]

_ASSERTION_SITE = (
    "python/test_library_api.py",
    'self.assertEqual(metadata["project"]["version"], "{version}")',
)


def _render(template: str, version: Version) -> str:
    return template.format(
        version=version,
        line=version.line,
        following=f"{version.major}.{version.minor + 1}",
    )


def _replace_exactly_once(path: Path, needle: str, replacement: str) -> None:
    """Rewrite a file, insisting the needle occurred exactly once.

    A zero-count replacement is the failure that matters: it leaves one manifest
    behind, and release.yml then rejects the release with three registries
    already half-considered. Failing here names the file instead.
    """
    if not path.is_file():
        raise BumpError(f"{path}: missing")
    text = path.read_text(encoding="utf-8")
    found = text.count(needle)
    if found != 1:
        raise BumpError(f"{path}: expected 1 occurrence of {needle!r}, found {found}")
    if needle != replacement:
        path.write_text(text.replace(needle, replacement), encoding="utf-8")


def _rewrite_sites(root: Path, sites: Sequence[tuple[str, str]], old: Version, new: Version) -> None:
    for relative, template in sites:
        _replace_exactly_once(
            root / relative, _render(template, old), _render(template, new)
        )


def read_manifest_version(root: Path) -> Version:
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    return Version.parse(package["version"])


def rewrite_manifests(root: Path, old: Version, new: Version) -> None:
    _rewrite_sites(root, _MANIFEST_SITES, old, new)


def rewrite_readme_pins(root: Path, old: Version, new: Version) -> None:
    """Move the six install pins onto the target's release line.

    Always run, not only on minor bumps. The pins carry MAJOR.MINOR only, so a
    patch bump renders the same text for old and new, leaves every README
    byte-identical, and still asserts each pin is present and on the expected
    line -- which is gate check 4 for free.
    """
    _rewrite_sites(root, _PIN_SITES, old, new)


def rewrite_version_assertion(root: Path, old: Version, new: Version) -> None:
    _rewrite_sites(root, [_ASSERTION_SITE], old, new)


def verify(root: Path, target: Version) -> None:
    """Reproduce release.yml's gate before anything is pushed.

    A GITHUB_TOKEN push starts no workflow run, so the pull request's checks
    never see the bump commit. Without this the bump would be verified for the
    first time during publishing. It re-reads from disk rather than trusting
    what the rewriters just did.
    """
    expected = str(target)

    # Check 1: the five values release.yml cross-checks, read the way it reads
    # them -- structurally, not by matching the string we hoped to write.
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
    found = {
        "package.json": package["version"],
        "package-lock.json (.version)": lock["version"],
        'package-lock.json (.packages[""].version)': lock["packages"][""]["version"],
    }
    for relative in ["python/pyproject.toml", "rust/Cargo.toml"]:
        match = re.search(
            r'^version = "([^"]+)"$',
            (root / relative).read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        found[relative] = match[1] if match else "<missing>"
    match = re.search(
        r'^name = "rtsp-backchannel"\nversion = "([^"]+)"$',
        (root / "rust" / "Cargo.lock").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    found["rust/Cargo.lock"] = match[1] if match else "<missing>"

    for where, value in found.items():
        if value != expected:
            raise BumpError(f"{where}: expected {expected}, found {value}")

    # Check 2: exactly one dated section, the same predicate release.yml applies.
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    dated = changelog.count(f"\n## [{expected}] - ")
    if dated != 1:
        raise BumpError(
            f"CHANGELOG.md: expected exactly 1 dated section for {expected}, found {dated}"
        )

    # Check 4: the six install pins and the hard-coded assertion.
    for relative, template in [*_PIN_SITES, _ASSERTION_SITE]:
        text = (root / relative).read_text(encoding="utf-8")
        if text.count(_render(template, target)) != 1:
            raise BumpError(f"{relative}: not on version {expected}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=python:. python3.14 -m unittest test_release_bump -v`
Expected: PASS, 46 tests.

- [ ] **Step 5: Prove the patterns match the real repository, not just the fixture**

Run:

```bash
PYTHONPATH=python:. python3.14 - <<'PY'
import pathlib, shutil, tempfile
from tools.bump_version import Version, read_manifest_version, rewrite_manifests, rewrite_readme_pins, rewrite_version_assertion, verify

with tempfile.TemporaryDirectory() as name:
    root = pathlib.Path(name) / "repo"
    for relative in ["package.json", "package-lock.json", "CHANGELOG.md", "README.md", "README.ko.md"]:
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(relative, root / relative)
    for relative in ["python/pyproject.toml", "python/README.md", "python/README.ko.md",
                     "python/test_library_api.py", "rust/Cargo.toml", "rust/Cargo.lock",
                     "rust/README.md", "rust/README.ko.md"]:
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(relative, root / relative)

    old = read_manifest_version(root)
    new = Version.parse("0.4.0")
    rewrite_manifests(root, old, new)
    rewrite_readme_pins(root, old, new)
    rewrite_version_assertion(root, old, new)
    print("manifests, pins and assertion rewrote cleanly against the real files:", old, "->", new)
PY
```

Expected: `manifests, pins and assertion rewrote cleanly against the real files: 0.3.1 -> 0.4.0`
If any pattern does not match the real file, this raises `BumpError` naming the file. Fix the pattern before continuing — the fixture passing is not evidence the real files match.

- [ ] **Step 6: Commit**

```bash
git add tools/bump_version.py python/test_release_bump.py
git commit -F - <<'EOF'
feat(release): rewrite every versioned file and verify the result

Eleven files carry the version and release.yml reads five of them through jq,
grep and an awk scan of Cargo.lock. Every substitution here asserts it matched
exactly once, because a zero-match is the failure that matters: it leaves one
manifest behind, and the gate then rejects the release with three registries
already half-considered. Failing at the substitution names the file instead.

The package-lock.json patterns anchor on surrounding structure rather than on
the version string, since a dependency could legitimately share the project's
version number. Cargo.toml's pattern anchors at line start so rust-version and
the inline dependency versions are not candidates.

The README pins run on every bump rather than only on minor ones. They carry
MAJOR.MINOR, so a patch bump substitutes a value for itself and leaves the six
files byte-identical -- while still asserting each pin exists and sits on the
expected line, which is gate check 4 for free.

verify() reproduces release.yml's gate by re-reading from disk. A GITHUB_TOKEN
push starts no workflow run, so the pull request's checks never see the bump
commit; without this the bump would be verified for the first time during
publishing.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
```

---

## Task 5: Git integration, orchestration, and the CLI

**Files:**
- Modify: `tools/bump_version.py`
- Modify (test): `python/test_release_bump.py`

**Interfaces:**
- Consumes: everything from Tasks 1–4.
- Produces:
  - `last_released(root: Path, manifest: Version) -> Version | None`
  - `commit_messages(root: Path, since: Version | None) -> list[str]`
  - `apply_bump(root: Path, *, today: str) -> dict` returning either
    `{"bumped": False, "reason": "empty-unreleased", "version": "0.3.1"}` or
    `{"bumped": True, "level": "minor", "previous": "0.3.1", "version": "0.4.0"}`
  - `main(argv: Sequence[str] | None = None) -> int`

- [ ] **Step 1: Write the failing tests**

Append to `python/test_release_bump.py` (add `import subprocess` at the top and extend the import line with `apply_bump, commit_messages, last_released, main`):

```python
def git(root: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def init_repo(root: pathlib.Path, *, version: str = "0.3.1", unreleased: str = "") -> None:
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    write_fixture_repo(root, version=version, unreleased=unreleased)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", f"chore(release): {version}")
    git(root, "tag", f"v{version}")


def commit(root: pathlib.Path, subject: str, body: str = "") -> None:
    message = f"{subject}\n\n{body}" if body else subject
    (root / "noise.txt").write_text(subject, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)


class GitIntegration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)

    def test_last_released_prefers_the_tag_matching_the_manifest(self):
        init_repo(self.root)
        self.assertEqual(last_released(self.root, Version.parse("0.3.1")), Version.parse("0.3.1"))

    def test_last_released_falls_back_when_the_manifest_is_already_ahead(self):
        init_repo(self.root)
        self.assertEqual(last_released(self.root, Version.parse("0.4.0")), Version.parse("0.3.1"))

    def test_last_released_is_none_without_tags(self):
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "t@e.com")
        git(self.root, "config", "user.name", "T")
        write_fixture_repo(self.root)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")

        self.assertIsNone(last_released(self.root, Version.parse("0.3.1")))

    def test_commit_messages_excludes_merges_and_reads_full_bodies(self):
        init_repo(self.root)
        commit(self.root, "fix(vigi): a fix")
        commit(self.root, "refactor: move things", "BREAKING CHANGE: it moved.")

        messages = commit_messages(self.root, Version.parse("0.3.1"))

        self.assertEqual(len(messages), 2)
        self.assertIn("BREAKING CHANGE: it moved.", messages[0] + messages[1])


class ApplyBump(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)

    def test_empty_unreleased_yields_no_bump_and_touches_nothing(self):
        init_repo(self.root)
        commit(self.root, "feat(cli): add a flag")
        before = {
            str(p.relative_to(self.root)): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file() and ".git" not in p.parts
        }

        result = apply_bump(self.root, today="2026-09-01")

        self.assertEqual(result, {"bumped": False, "reason": "empty-unreleased", "version": "0.3.1"})
        after = {
            str(p.relative_to(self.root)): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file() and ".git" not in p.parts
        }
        self.assertEqual(before, after)

    def test_feature_commit_produces_a_minor_bump(self):
        init_repo(self.root, unreleased="### Added\n\n- A transport.")
        commit(self.root, "feat(cli): add --transport")

        result = apply_bump(self.root, today="2026-09-01")

        self.assertEqual(result["bumped"], True)
        self.assertEqual(result["level"], "minor")
        self.assertEqual(result["previous"], "0.3.1")
        self.assertEqual(result["version"], "0.4.0")
        self.assertEqual(read_manifest_version(self.root), Version.parse("0.4.0"))
        self.assertIn("rtsp-backchannel@^0.4", (self.root / "README.md").read_text())

    def test_fix_only_produces_a_patch_bump_and_leaves_readmes_alone(self):
        init_repo(self.root, unreleased="### Fixed\n\n- A bug.")
        before = (self.root / "README.md").read_bytes()
        commit(self.root, "fix(vigi): a fix")

        result = apply_bump(self.root, today="2026-09-01")

        self.assertEqual(result["version"], "0.3.2")
        self.assertEqual((self.root / "README.md").read_bytes(), before)

    def test_result_satisfies_the_release_gate(self):
        init_repo(self.root, unreleased="### Added\n\n- A transport.")
        commit(self.root, "feat(cli): add --transport")

        apply_bump(self.root, today="2026-09-01")

        verify(self.root, Version.parse("0.4.0"))

    def test_promoted_changelog_is_dated_and_unreleased_is_emptied(self):
        init_repo(self.root, unreleased="### Added\n\n- A transport.")
        commit(self.root, "feat(cli): add --transport")

        apply_bump(self.root, today="2026-09-01")

        text = (self.root / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("## [0.4.0] - 2026-09-01", text)
        self.assertIn("## [Unreleased]\n\n## [0.4.0]", text)
        self.assertIn("- A transport.", text)

    def test_rerunning_with_no_new_work_changes_nothing(self):
        init_repo(self.root, unreleased="### Added\n\n- A transport.")
        commit(self.root, "feat(cli): add --transport")
        apply_bump(self.root, today="2026-09-01")
        after_first = {
            str(p.relative_to(self.root)): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file() and ".git" not in p.parts
        }

        result = apply_bump(self.root, today="2026-09-02")

        self.assertEqual(result["bumped"], False)
        after_second = {
            str(p.relative_to(self.root)): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file() and ".git" not in p.parts
        }
        self.assertEqual(after_first, after_second)

    def test_rerun_with_a_feature_raises_a_staged_patch_to_a_minor(self):
        init_repo(self.root, unreleased="### Fixed\n\n- A bug.")
        commit(self.root, "fix(vigi): a fix")
        apply_bump(self.root, today="2026-09-01")
        self.assertEqual(read_manifest_version(self.root), Version.parse("0.3.2"))

        # The pull request gains a feature and a fresh changelog entry.
        path = self.root / "CHANGELOG.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "## [Unreleased]\n\n", "## [Unreleased]\n\n### Added\n\n- A flag.\n\n", 1
            ),
            encoding="utf-8",
        )
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "chore(release): bump version to 0.3.2")
        commit(self.root, "feat(cli): add --transport")

        result = apply_bump(self.root, today="2026-09-02")

        self.assertEqual(result["version"], "0.4.0")
        self.assertEqual(read_manifest_version(self.root), Version.parse("0.4.0"))
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count("## [0.4.0] - "), 1)
        self.assertNotIn("## [0.3.2]", text)
        self.assertIn("- A bug.", text)
        self.assertIn("- A flag.", text)
        self.assertIn("rtsp-backchannel@^0.4", (self.root / "README.md").read_text())
        verify(self.root, Version.parse("0.4.0"))


class CommandLine(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)

    def test_prints_a_json_summary_and_exits_zero(self):
        import contextlib
        import io

        init_repo(self.root, unreleased="### Added\n\n- A transport.")
        commit(self.root, "feat(cli): add --transport")

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["--root", str(self.root), "--today", "2026-09-01"])

        self.assertEqual(code, 0)
        summary = json.loads(stdout.getvalue())
        self.assertEqual(summary["bumped"], True)
        self.assertEqual(summary["version"], "0.4.0")

    def test_reports_a_bad_tree_on_stderr_and_exits_nonzero(self):
        import contextlib
        import io

        init_repo(self.root, unreleased="### Added\n\n- A transport.")
        (self.root / "rust" / "README.md").write_text("# no pin\n", encoding="utf-8")
        commit(self.root, "feat(cli): add --transport")

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main(["--root", str(self.root), "--today", "2026-09-01"])

        self.assertEqual(code, 1)
        self.assertIn("README.md", stderr.getvalue())
```

Add `import json` to the test file's imports.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=python:. python3.14 -m unittest test_release_bump -v`
Expected: FAIL — `ImportError: cannot import name 'apply_bump'`

- [ ] **Step 3: Write the minimal implementation**

Add `import argparse`, `import subprocess`, `import sys`, and `from datetime import datetime, timezone` to `tools/bump_version.py`, then append:

```python
def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise BumpError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _tag_exists(root: Path, tag: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def last_released(root: Path, manifest: Version) -> Version | None:
    """The version the last release actually shipped.

    Normally the manifest holds it and release.yml tagged it. On a branch that
    already carries a bump commit the manifest is ahead of every tag, so fall
    back to the most recent tag reachable from HEAD. A repository with no tags
    at all yields None and the caller scans the whole history.
    """
    if _tag_exists(root, f"v{manifest}"):
        return manifest
    result = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0", "--match", "v*"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Version.parse(result.stdout.strip().removeprefix("v"))


def commit_messages(root: Path, since: Version | None) -> list[str]:
    """Full commit messages since a release, merges excluded.

    Merge commits are the only non-conventional subjects in this history, and
    NUL record separators keep multi-line bodies intact so a BREAKING CHANGE
    footer is not split across records.
    """
    rev_range = f"v{since}..HEAD" if since is not None else "HEAD"
    out = _git(root, "log", "--no-merges", "--format=%B%x00", rev_range)
    return [message.strip() for message in out.split("\0") if message.strip()]


def apply_bump(root: Path, *, today: str) -> dict:
    manifest = read_manifest_version(root)
    changelog = Changelog.parse((root / "CHANGELOG.md").read_text(encoding="utf-8"))

    if not changelog.pending():
        # The hand-written [Unreleased] section is what declares a release. An
        # empty one means nothing to ship -- and after a bump it is empty, which
        # is also what makes a re-run a no-op.
        return {"bumped": False, "reason": "empty-unreleased", "version": str(manifest)}

    released = last_released(root, manifest)
    base = released if released is not None else manifest
    level = level_from_commits(commit_messages(root, released), major=base.major)
    target = base.bumped(level)

    (root / "CHANGELOG.md").write_text(
        changelog.promoted(target=target, last_released=base, today=today).render(),
        encoding="utf-8",
    )
    rewrite_manifests(root, manifest, target)
    rewrite_readme_pins(root, manifest, target)
    rewrite_version_assertion(root, manifest, target)

    verify(root, target)

    return {
        "bumped": True,
        "level": level,
        "previous": str(base),
        "version": str(target),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root (default: %(default)s)")
    parser.add_argument(
        "--today",
        default=None,
        help="release date as YYYY-MM-DD (default: today, UTC)",
    )
    args = parser.parse_args(argv)

    today = args.today or datetime.now(timezone.utc).date().isoformat()
    try:
        summary = apply_bump(Path(args.root), today=today)
    except BumpError as error:
        print(f"bump_version: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=python:. python3.14 -m unittest test_release_bump -v`
Expected: PASS, 59 tests.

- [ ] **Step 5: Run the whole Python suite on both supported versions**

Run: `PYTHONPATH=python:. python3.14 -m unittest discover -s python -p 'test_*.py'`
Expected: PASS — the new tests plus the existing suite, no regressions.

- [ ] **Step 6: Dry-run against a real copy of this repository**

Run:

```bash
rm -rf /tmp/bump-dryrun && git clone -q --no-hardlinks . /tmp/bump-dryrun \
  && git -C /tmp/bump-dryrun fetch -q origin 'refs/tags/*:refs/tags/*' \
  && PYTHONPATH=python:. python3.14 tools/bump_version.py --root /tmp/bump-dryrun --today 2026-09-01 \
  && git -C /tmp/bump-dryrun --no-pager diff --stat
```

Expected: `{"bumped": true, "level": "minor", "previous": "0.3.1", "version": "0.4.0"}` followed by a diff touching 13 files. Read the `CHANGELOG.md` hunk and confirm the hand-written `[Unreleased]` prose moved intact under `## [0.4.0] - 2026-09-01`.

- [ ] **Step 7: Commit**

```bash
git add tools/bump_version.py python/test_release_bump.py
git commit -F - <<'EOF'
feat(release): resolve the last release and apply the bump end to end

Two signals with two jobs: the hand-written [Unreleased] section decides
whether a release happens, and the conventional-commit log decides how far the
version moves. Reading releasability out of commit types alone leaves
docs-only changes permanently ambiguous in a repository that ships its READMEs
inside all three packages.

last_released prefers the tag matching the manifest, which is the normal case,
and falls back to the newest reachable tag on a branch that already carries a
bump commit. The commit scan uses NUL record separators so a BREAKING CHANGE
footer is not split across records, and --no-merges because merge commits are
the only non-conventional subjects in this history.

Everything is computed against the last release rather than against the
current manifest, so a pull request that gains a feat after its first bump
recomputes to the same end state instead of bumping twice.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
```

---

## Task 6: The pull request workflow

**Files:**
- Create: `.github/workflows/version-bump.yml`

**Interfaces:**
- Consumes: `tools/bump_version.py` via `python tools/bump_version.py --root .`, which prints `{"bumped": bool, "version": str, ...}` on stdout and exits non-zero on a malformed tree.
- Produces: a commit on `dev` titled `chore(release): bump version to X.Y.Z`.

- [ ] **Step 1: Write the workflow**

Create `.github/workflows/version-bump.yml`:

```yaml
name: Version bump

# Raises the version on a dev -> master pull request so that merging it
# publishes a release. release.yml only publishes when a manifest version
# differs from what is live on npm, PyPI and crates.io; nothing else computes
# that difference, so without this a merge is a silent no-op.
#
# The bump lands on dev, not on master after the merge. Bumping on master
# strands dev at the old version, so the next cycle either conflicts across all
# five manifests or presents a diff that lowers the version.

on:
  pull_request:
    types: [opened, synchronize, reopened]

permissions:
  contents: read

concurrency:
  group: version-bump-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  bump:
    name: Bump version for release
    if: >-
      github.repository == 'GagaKor/rtsp-backchannel' &&
      github.base_ref == 'master' &&
      github.head_ref == 'dev'
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      contents: write
    steps:
      # The head branch itself, not the merge commit: the bump is pushed back to
      # dev. Full depth because the bump reads every commit since the last tag.
      - uses: actions/checkout@v7
        with:
          ref: ${{ github.head_ref }}
          fetch-depth: 0

      - uses: actions/setup-python@v7
        with:
          python-version: '3.11'

      - name: Compute and apply the version bump
        id: bump
        run: |
          set -euo pipefail
          summary=$(python tools/bump_version.py --root .)
          echo "$summary"
          echo "bumped=$(printf '%s' "$summary" | jq -r '.bumped')" >> "$GITHUB_OUTPUT"
          echo "version=$(printf '%s' "$summary" | jq -r '.version')" >> "$GITHUB_OUTPUT"
          printf '%s\n' "$summary" >> "$GITHUB_STEP_SUMMARY"

      # A GITHUB_TOKEN push starts no workflow run, so the pull request's own
      # checks will not re-run against the bump commit. This is the only place
      # the rewritten version assertion is exercised before master.
      - name: Verify the bumped tree
        if: steps.bump.outputs.bumped == 'true'
        run: PYTHONPATH=python:. python -m unittest test_library_api

      - name: Commit and push the bump
        if: steps.bump.outputs.bumped == 'true'
        run: |
          set -euo pipefail
          git config user.name 'github-actions[bot]'
          git config user.email '41898282+github-actions[bot]@users.noreply.github.com'
          git add \
            package.json \
            package-lock.json \
            python/pyproject.toml \
            python/test_library_api.py \
            rust/Cargo.toml \
            rust/Cargo.lock \
            CHANGELOG.md \
            README.md \
            README.ko.md \
            python/README.md \
            python/README.ko.md \
            rust/README.md \
            rust/README.ko.md
          if git diff --cached --quiet; then
            echo 'Version already at ${{ steps.bump.outputs.version }}; nothing to commit.'
            exit 0
          fi
          git commit -m 'chore(release): bump version to ${{ steps.bump.outputs.version }}'
          git push origin "HEAD:${{ github.head_ref }}"
```

- [ ] **Step 2: Validate the workflow parses**

Run:

```bash
python3.14 -c "
import json, sys
try:
    import yaml
except ImportError:
    sys.exit('PyYAML not installed; run: python3.14 -m pip install --user pyyaml')
doc = yaml.safe_load(open('.github/workflows/version-bump.yml'))
job = doc['jobs']['bump']
assert job['permissions']['contents'] == 'write', job['permissions']
assert [s.get('name') or s.get('uses') for s in job['steps']]
print('workflow parses; steps:', [s.get('name') or s.get('uses') for s in job['steps']])
"
```

Expected: the step list prints. Note that PyYAML parses `on:` as the boolean key `True` — that is a YAML quirk, not a workflow bug; do not "fix" it by quoting `on`.

- [ ] **Step 3: Confirm the guard expression cannot fire on the wrong PR**

Run:

```bash
grep -n -A4 "if: >-" .github/workflows/version-bump.yml
```

Expected: all three of `github.repository == 'GagaKor/rtsp-backchannel'`, `github.base_ref == 'master'`, and `github.head_ref == 'dev'` are present and joined with `&&`. A Dependabot PR into `master` fails the `head_ref` test; a feature PR into `dev` fails the `base_ref` test.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/version-bump.yml
git commit -F - <<'EOF'
feat(ci): bump the version on dev -> master pull requests

release.yml publishes only when a manifest version differs from what is live
on the three registries, and nothing computed that difference, so merging dev
into master shipped nothing. This runs the bump on the pull request and pushes
one reviewable commit to dev.

It checks out the head branch rather than the merge commit because the bump is
pushed back to dev, and at full depth because the level is read from every
commit since the last tag.

The unittest step is not redundant with ci.yml. A GITHUB_TOKEN push starts no
workflow run, so the pull request's checks never re-run against the bump
commit; this is the only place the rewritten version assertion is exercised
before master. The paths passed to git add are listed rather than using -A so
the bot cannot commit anything the bump did not produce.

release.yml is untouched: its path and the release environment name are
registered with npm, PyPI and crates.io as Trusted Publisher configuration.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
```

---

## Task 7: Documentation

**Files:**
- Modify: `RELEASING.md` (the "Automated releases (the normal path)" section and the "To ship a release" list)
- Modify: `CONTRIBUTING.md` (append a section)
- Create: `docs/decisions/2026-09-01-automate-version-bump-on-release-prs.md`

**Interfaces:**
- Consumes: the behaviour built in Tasks 1–6.
- Produces: no code.

- [ ] **Step 1: Rewrite the "To ship a release" list in `RELEASING.md`**

Replace the existing three-item list under "To ship a release:" with:

```markdown
To ship a release:

1. Write what changed under `## [Unreleased]` in `CHANGELOG.md`, as part of the
   work itself. This is what declares a release: an empty `[Unreleased]` means
   the merge publishes nothing.
2. Open a pull request from `dev` to `master`.
3. `.github/workflows/version-bump.yml` computes the next version from the
   conventional-commit types since the last tag, rewrites every versioned file,
   verifies the result against the same gate `release.yml` applies, and pushes
   one `chore(release): bump version to X.Y.Z` commit to `dev`.
4. Review that commit — the version and the newly dated changelog section — and
   merge.
5. `release.yml` runs on the merge, pins the tag and provenance draft, publishes
   whichever packages changed version, and completes the GitHub Release.

Nothing in step 1 requires editing a version by hand. The manual process in
"1. Prepare the release" below is the fallback for when the workflow cannot run.
```

- [ ] **Step 2: Correct the false claim about the test suite in `RELEASING.md`**

In "1. Prepare the release", step 2 currently reads "Update the version references that live outside the manifests, or the test suite will fail: the install pins in all six READMEs … and the asserted version in `python/test_library_api.py`". Replace that opening clause with:

```markdown
2. Update the version references that live outside the manifests. Only one of
   them is enforced by the test suite — the asserted version in
   `python/test_library_api.py::test_declares_installable_wheel_metadata`. The
   install pins in all six READMEs (`rtsp-backchannel@^X.Y` for npm,
   `'rtsp-backchannel>=X.Y,<X.Z'` for pip, `rtsp-backchannel = "X.Y"` for Cargo)
   are checked by `tools/bump_version.py` but by no test, so a hand-edited
   release can ship a stale pin.
```

- [ ] **Step 3: Verify that correction against the code**

Run:

```bash
grep -rn "rtsp-backchannel@\^\|rtsp-backchannel>=\|rtsp-backchannel = \"" \
  --include='*.ts' --include='test_*.py' --include='*.rs' src python rust || \
  echo "confirmed: no test asserts the README install pins"
```

Expected: `confirmed: no test asserts the README install pins`

- [ ] **Step 4: Append the release-mechanics section to `CONTRIBUTING.md`**

```markdown
## Commit messages and releases

Commit subjects follow [Conventional Commits](https://www.conventionalcommits.org/)
— `type(scope): description`. Nothing enforces this mechanically, and two parts
of the release now depend on it:

- **`feat`, and any `!` or `BREAKING CHANGE:` footer, move the minor version.**
  Everything else moves the patch version. While the major version is 0, a
  breaking change moves the minor rather than the major: at 0.x the minor
  position is the compatibility boundary, and the install pins already stop at
  the next minor. Nothing promotes the project to 1.0.0 automatically.
- **`## [Unreleased]` in `CHANGELOG.md` decides whether a release happens at
  all.** Write the entry as part of the change. An empty `[Unreleased]` means a
  `dev` → `master` merge publishes nothing, which is the correct outcome for
  work that should not ship on its own.

Merging `dev` into `master` then bumps the version and publishes automatically.
See `RELEASING.md`.
```

- [ ] **Step 5: Write the ADR**

Create `docs/decisions/2026-09-01-automate-version-bump-on-release-prs.md`:

```markdown
# Bump the version on the dev → master pull request, not after the merge

**Date:** 2026-09-01
**Status:** Accepted

## Context

`release.yml` publishes to npm, PyPI and crates.io on every push to `master`,
but only for packages whose manifest version differs from the version already
live on that registry. Nothing computed that difference. A merge carrying no
hand-written bump was not an error — it was a silent no-op that shipped
nothing, which is how the VIGI transport, CommonJS support and the `audioSend`
capability report all accumulated under `[Unreleased]` while 0.3.1 stayed live
on all three registries.

A version lives in eleven files: five manifests and lock files that the release
gate cross-checks, six README install pins, one hard-coded test assertion, and
the changelog. Moving them by hand is the step that got skipped.

## Decision

A `pull_request` workflow on `dev` → `master` computes the next version, rewrites
every versioned file, verifies the result against the release gate, and pushes
one commit to `dev`. The pull request carries the bump; merging it publishes.

## Alternatives Considered

### Bump on the pull request (chosen)

The bump lands on `dev` before the merge, so `master` never exists in a state
that needs a follow-up bump, and `dev` and `master` cannot drift apart. A human
reviews the version and the dated changelog section in the same diff as the
work. `release.yml` is not modified.

The cost is that the bump happens when the PR opens rather than when it merges,
so work added afterwards triggers a recompute — handled by recomputing the whole
end state from the last release rather than patching the previous run's output.

### Bump on master after the merge

Rejected. It leaves `dev` at the old version, so the next `dev` → `master` pull
request either conflicts across all five manifests or presents a diff that
*lowers* the version; closing that hole needs a second automation to sync
`master` back into `dev`. It also needs `release.yml` to be re-invoked through
`workflow_dispatch`, because a push made with `GITHUB_TOKEN` triggers no
workflow run, and it breaks the day `master` gains branch protection.

### Bump inside release.yml

Rejected. One workflow instead of two, but the pinned-SHA, tag and
draft-provenance logic all assume `github.sha` is the released commit.
Reworking a pipeline that already publishes correctly to three registries over
OIDC trusted publishing is risk without a matching gain, and the file's path and
environment name are registered with each registry.

## Reasoning

Two signals with two distinct jobs. The hand-written `[Unreleased]` section
decides *whether* to release; the conventional-commit log decides *by how much*.
Deciding releasability from commit types alone leaves `docs`-only changes
permanently ambiguous in a repository that ships its READMEs inside all three
packages — sometimes a docs change is worth a release and sometimes it is not.
The changelog is where a human already states that intent.

Breaking changes map to minor while the major version is 0. The install pins
(`^0.3`, `>=0.3,<0.4`, `"0.3"`) already treat the minor position as the
compatibility boundary, so a consumer pinned to 0.3 does not silently receive a
breaking 0.4. Declaring 1.0.0 is a claim automation should not make.

## Trade-offs Accepted

- The bot pushes directly to `dev`. Neither branch is protected today; if `dev`
  gains protection the bot needs an exception.
- A `GITHUB_TOKEN` push starts no workflow run, so the pull request's checks do
  not re-run against the bump commit. The bump job therefore reproduces the
  release gate itself and fails before pushing.
- Commit-type discipline becomes load-bearing without mechanical enforcement. A
  `feat` mislabeled as `fix` produces a patch release of a feature; the changelog
  review in the pull request is the backstop.

## Related Code Paths

- `tools/bump_version.py`
- `.github/workflows/version-bump.yml`
- `.github/workflows/release.yml` (unmodified; consumes the bumped commit)
- `docs/superpowers/specs/2026-09-01-automated-version-bump-design.md`

## Consequences

Releasing becomes: write the changelog entry, open the pull request, review one
generated commit, merge. The version references that used to be updated by hand
— and were the step that got skipped — are no longer a manual step at all.
```

- [ ] **Step 6: Commit**

```bash
git add RELEASING.md CONTRIBUTING.md docs/decisions/2026-09-01-automate-version-bump-on-release-prs.md
git commit -F - <<'EOF'
docs: document the automated release flow

RELEASING.md described a manual version bump as the normal path and the
workflow as merely publishing what it found. That is now inverted: the pull
request produces the bump and the manual steps are the fallback.

Its claim that stale README pins fail the test suite was also wrong, and wrong
in the direction that matters -- it invited trusting CI to catch a stale pin.
No test asserts the pins; only python/test_library_api.py hard-codes the
version. tools/bump_version.py checks them now, but a hand-edited release
still would not.

CONTRIBUTING.md gains the two conventions that are now load-bearing and
mechanically unenforced: commit types set the version, and [Unreleased]
decides whether a release happens at all.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
```

---

## Final verification

- [ ] **Run the complete test suite**

```bash
PYTHONPATH=python:. python3.14 -m unittest discover -s python -p 'test_*.py'
npm test
npm run typecheck
```

Expected: all green. The TypeScript and Rust suites are untouched by this work; run them to confirm that.

- [ ] **Confirm release.yml was not modified**

```bash
git diff --stat origin/dev -- .github/workflows/release.yml
```

Expected: empty output.

- [ ] **Confirm the full change set**

```bash
git diff --stat origin/dev
```

Expected: `tools/bump_version.py`, `python/test_release_bump.py`, `.github/workflows/version-bump.yml`, `RELEASING.md`, `CONTRIBUTING.md`, and the two docs files. Nothing under `src/`, `rust/src/`, or `python/rtsp_backchannel/`.

- [ ] **Open the pull request into `dev`**

This branch merges into `dev` first. The dev → master pull request that follows is the one that exercises the new workflow, and it will produce the 0.4.0 bump.
