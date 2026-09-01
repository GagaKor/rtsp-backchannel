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
