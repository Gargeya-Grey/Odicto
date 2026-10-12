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


if __name__ == "__main__":
    unittest.main()
