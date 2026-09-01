"""Tests for tools/bump_version.py."""

import pathlib
import unittest

from tools.bump_version import BumpError, Changelog, Version, level_from_commits


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


if __name__ == "__main__":
    unittest.main()
