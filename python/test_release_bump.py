"""Tests for tools/bump_version.py."""

import json
import pathlib
import subprocess
import tempfile
import unittest

from tools.bump_version import (
    BumpError,
    Changelog,
    GITHUB_REPO_URL,
    Version,
    apply_bump,
    commit_messages,
    last_released,
    level_from_commits,
    main,
    read_manifest_version,
    rewrite_manifests,
    rewrite_readme_pins,
    rewrite_version_assertion,
    verify,
)


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
        # Move the manifests, pins and assertion to the target but leave the
        # changelog behind, so check 1 passes and check 2 is what fails.
        old, new = Version.parse("0.3.1"), Version.parse("0.4.0")
        rewrite_manifests(self.root, old, new)
        rewrite_readme_pins(self.root, old, new)
        rewrite_version_assertion(self.root, old, new)

        with self.assertRaises(BumpError) as caught:
            verify(self.root, new)

        self.assertIn("CHANGELOG.md", str(caught.exception))

    def test_a_tree_consistently_on_the_wrong_version_fails_verification(self):
        """Agreeing with each other is not enough; the five must equal the target."""
        with self.assertRaises(BumpError) as caught:
            verify(self.root, Version.parse("0.3.2"))

        self.assertIn("package.json", str(caught.exception))

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


def init_dev_master_merge_topology(root: pathlib.Path) -> None:
    """Build the topology this repository actually has, not a linear branch.

    Two releases, each landing as a dev -> master merge commit tagged by
    release.yml. Only the first is ever merged forward into dev afterwards --
    master then drifts on its own, exactly as a dependency-bump PR merged
    straight into master does in the real repository. So v0.3.0 ends up
    reachable from dev, but v0.3.1 -- the more recent release -- does not.
    HEAD ends up on dev.
    """
    git(root, "init", "-q", "-b", "master")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    write_fixture_repo(root, version="0.2.0")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "chore: bootstrap")

    git(root, "checkout", "-q", "-b", "dev")
    write_fixture_repo(root, version="0.3.0")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "chore(release): bump version to 0.3.0")

    git(root, "checkout", "-q", "master")
    git(root, "merge", "-q", "--no-ff", "-m", "Merge pull request #1 from dev", "dev")
    git(root, "tag", "v0.3.0")

    # The one merge-forward: master (now carrying v0.3.0) is merged back into
    # dev, so the tag becomes reachable from dev too.
    git(root, "checkout", "-q", "dev")
    git(root, "merge", "-q", "-m", "Merge master back into dev", "master")

    commit(root, "feat(cli): add --transport")
    write_fixture_repo(root, version="0.3.1")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "chore(release): bump version to 0.3.1")

    git(root, "checkout", "-q", "master")
    git(root, "merge", "-q", "--no-ff", "-m", "Merge pull request #2 from dev", "dev")
    git(root, "tag", "v0.3.1")

    # master drifts on its own after this release -- e.g. a dependency bump PR
    # merged straight into master -- and is never merged forward into dev.
    (root / "MASTER_ONLY.txt").write_text("drift\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "chore(deps): bump something directly on master")

    git(root, "checkout", "-q", "dev")


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

    def test_last_released_finds_the_merge_commit_tag_from_dev_even_when_unreachable(self):
        """release.yml tags the dev -> master merge commit, which is never an
        ancestor of dev. From dev, `git describe --tags --abbrev=0` cannot see
        v0.3.1 at all -- and because an older tag (v0.3.0, merged forward once)
        is reachable, `git describe` returns that stale tag instead of failing
        outright. Resolution has to be by tag existence, never reachability.
        """
        init_dev_master_merge_topology(self.root)

        self.assertEqual(
            last_released(self.root, Version.parse("0.3.2")), Version.parse("0.3.1")
        )


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

    def test_apply_bump_uses_the_newest_release_not_an_older_reachable_one(self):
        """Regression for the dev/master merge topology: v0.3.1's tag lives on
        a merge commit that is never an ancestor of dev, while the older
        v0.3.0 happens to be reachable (merged forward once, then master
        drifted on its own). A reachability-based fallback picks v0.3.0 and
        pulls the feat that justified the 0.3.0 -> 0.3.1 release back into
        range, computing "minor" from a base two releases stale and landing on
        0.4.0. Existence-based resolution finds v0.3.1 and a patch-only range,
        landing on 0.3.2.
        """
        init_dev_master_merge_topology(self.root)

        # A patch-only commit closes out the first release cycle on dev.
        path = self.root / "CHANGELOG.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "## [Unreleased]\n\n", "## [Unreleased]\n\n### Fixed\n\n- A late fix.\n\n", 1
            ),
            encoding="utf-8",
        )
        commit(self.root, "fix(vigi): a late fix")

        apply_bump(self.root, today="2026-09-01")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "chore(release): bump version to 0.3.2")

        # The PR re-triggers with one more patch-only commit -- the rerun that
        # exercises last_released's fallback, since the manifest (0.3.2) is
        # not yet tagged.
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "## [Unreleased]\n\n", "## [Unreleased]\n\n### Fixed\n\n- Another late fix.\n\n", 1
            ),
            encoding="utf-8",
        )
        commit(self.root, "fix(cli): another late fix")

        result = apply_bump(self.root, today="2026-09-02")

        self.assertEqual(result["previous"], "0.3.1")
        self.assertEqual(result["level"], "patch")
        self.assertEqual(result["version"], "0.3.2")
        verify(self.root, Version.parse("0.3.2"))


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


if __name__ == "__main__":
    unittest.main()
