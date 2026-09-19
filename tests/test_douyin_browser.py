import unittest

import douyin_browser as db


class BrowserChannelTests(unittest.TestCase):
    def test_edge_is_the_default(self):
        self.assertEqual(db._normalize_browser_channel(None), "msedge")

    def test_edge_aliases_are_normalized(self):
        self.assertEqual(db._normalize_browser_channel("edge"), "msedge")
        self.assertEqual(db._normalize_browser_channel(" Microsoft-Edge "), "msedge")

    def test_supported_alternatives_are_preserved(self):
        self.assertEqual(db._normalize_browser_channel("chrome"), "chrome")
        self.assertEqual(db._normalize_browser_channel("chromium"), "chromium")

    def test_unknown_channel_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "只支持"):
            db._normalize_browser_channel("firefox")


if __name__ == "__main__":
    unittest.main()
