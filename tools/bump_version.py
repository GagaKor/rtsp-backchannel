#!/usr/bin/env python3
"""Compute and apply the next release version across every versioned file.

Run from the repository root:

    python tools/bump_version.py --root .

Prints a one-line JSON summary on stdout. Exits 0 whether or not a bump
happened; exits non-zero only when a file it must edit is not in the shape it
expects, which is a condition a human has to look at.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

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

    Resolved by tag existence, never by reachability. release.yml tags the
    dev -> master merge commit, and a merge commit is never an ancestor of the
    branch that was merged, so a release tag is never reachable from dev.
    `git describe` would therefore either fail outright or, worse, silently
    return an older tag that is reachable and compute the next version from a
    base two releases stale.
    """
    if _tag_exists(root, f"v{manifest}"):
        return manifest

    earlier = []
    for line in _git(root, "tag", "--list", "v*").splitlines():
        try:
            version = Version.parse(line.strip().removeprefix("v"))
        except BumpError:
            continue
        if version < manifest:
            earlier.append(version)
    return max(earlier) if earlier else None


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
