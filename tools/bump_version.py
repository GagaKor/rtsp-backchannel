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
