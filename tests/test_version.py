"""The VERSION file is the one source of Odicto's version; releases are tagged v<VERSION>."""
import unittest

import version
from paths import ROOT


class TestVersion(unittest.TestCase):
    def test_version_file_is_semver(self):
        text = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertRegex(text, version.VERSION_RE)
        self.assertEqual(text, version.app_version())

    def test_missing_or_malformed_file_reads_as_zero(self):
        from unittest import mock

        with mock.patch.object(version, "VERSION_PATH", ROOT / "no-such-VERSION"):
            self.assertEqual("0.0.0", version.app_version())
        with mock.patch.object(version.VERSION_PATH.__class__, "read_text", return_value="v1.2"):
            self.assertEqual("0.0.0", version.app_version())

    def test_changelog_has_a_section_for_this_version(self):
        """The Release workflow publishes this section; it refuses a version without one."""
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        heading = "## [%s]" % version.app_version()
        self.assertIn("\n" + heading, changelog, "add a %s section to CHANGELOG.md" % heading)

    def test_release_workflow_reads_the_version_file(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("Get-Content VERSION", workflow)
        self.assertIn("refs/heads/main", workflow)
        self.assertTrue((ROOT / "tools" / "release" / "release-notes.md").is_file())


if __name__ == "__main__":
    unittest.main()
