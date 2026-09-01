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
