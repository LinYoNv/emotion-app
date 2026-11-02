import unittest

import companion_v2.adapters.astrbot.native as native
from companion_v2.adapters.astrbot.native import check_version


class AstrBotNativeVersionTests(unittest.TestCase):
    def test_reviewed_releases_are_silent(self):
        for version in ("4.27.2", "4.27.5", "4.28.2", "4.29.0", "4.29.1", "4.29.9",
                        "4.29.0-beta.1"):
            with self.subTest(version=version):
                native._REPORTED_UNVERIFIED.discard(version)
                with self.assertNoLogs("companion_v2.adapters.astrbot.native", level="WARNING"):
                    check_version(version)

    def test_unreviewed_hosts_still_start_with_one_warning(self):
        for version in ("4.30.0", "5.0.0", "3.5.27", "unknown"):
            with self.subTest(version=version):
                native._REPORTED_UNVERIFIED.discard(version)
                with self.assertLogs("companion_v2.adapters.astrbot.native", level="WARNING") as captured:
                    check_version(version)
                    check_version(version)
                self.assertEqual(len(captured.records), 1)
                self.assertIn(version, captured.records[0].getMessage())
                self.assertIn("loading anyway", captured.records[0].getMessage())

    def test_unparsable_versions_never_block_loading(self):
        for version in (None, [], 429, 4.29, "", "4", "abc"):
            with self.subTest(version=version):
                native._REPORTED_UNVERIFIED.discard("unknown")
                native._REPORTED_UNVERIFIED.discard(version if type(version) is str else "unknown")
                check_version(version)

    def test_older_and_newer_hosts_are_not_refused(self):
        for version in ("4.27.1", "4.26.8", "4.20.0", "3.5.27", "5.0.0", "5.1.0"):
            with self.subTest(version=version):
                native._REPORTED_UNVERIFIED.discard(version)
                check_version(version)


if __name__ == "__main__":
    unittest.main()
