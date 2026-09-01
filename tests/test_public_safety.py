from __future__ import annotations

import asyncio
import builtins
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import douyin_collects as dc
import ingest
import server


class IndexedArtifactSafetyTests(unittest.TestCase):
    def test_missing_indexed_file_is_not_known(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = ingest.Library(Path(tmp))
            lib.record({"video_id": "stale", "path": "inbox/missing.md"})
            self.assertIsNone(lib.known("stale"))

    def test_existing_relative_indexed_file_is_known(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lib = ingest.Library(root)
            note = root / "inbox" / "saved.md"
            note.write_text("placeholder", encoding="utf-8")
            lib.record({"video_id": "saved", "path": "inbox/saved.md"})
            self.assertEqual(lib.known("saved")["video_id"], "saved")


class PendingUncollectSafetyTests(unittest.TestCase):
    def test_incomplete_recount_keeps_queue_and_does_not_uncollect(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = ingest.Library(Path(tmp))
            lib.queue_uncollect([
                {"aweme_id": "queued", "url": "https://www.douyin.com/video/1"}
            ])
            uncollect = AsyncMock()
            with patch.object(
                dc,
                "fetch_favorites",
                AsyncMock(return_value=({"complete": False}, [{"aweme_id": "queued"}])),
            ), patch.object(dc, "uncollect_items", uncollect):
                code = asyncio.run(ingest._run_pending_uncollect(lib, force_now=True))
            self.assertEqual(code, 1)
            uncollect.assert_not_awaited()
            self.assertEqual(
                [row["aweme_id"] for row in lib.pending_uncollect()], ["queued"]
            )


class EmptyContentSafetyTests(unittest.TestCase):
    def test_generated_image_headings_are_not_effective_ocr(self):
        self.assertFalse(
            ingest._has_effective_image_ocr(
                "### 图 1\n\n（这张图没识别出文字）\n\n"
                "### 图 2\n\n（识别失败：RuntimeError）"
            )
        )
        self.assertTrue(ingest._has_effective_image_ocr("### 图 1\n\n有效文字"))


class PlatformDetectionTests(unittest.TestCase):
    def test_allows_real_domains(self):
        cases = {
            "https://douyin.com/path": "douyin",
            "https://www.douyin.com:443/path": "douyin",
            "https://a.iesdouyin.com/path": "douyin",
            "https://bilibili.com/video/BV1": "bilibili",
            "https://B23.TV/xxxxxx": "bilibili",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(server._detect_platform(url), expected)

    def test_rejects_lookalike_domains(self):
        rejected = [
            "https://fake-douyin.com/path",
            "https://douyin.com.example.org/path",
            "https://bilibili.com.evil.test/path",
            "https://b23.tv.evil.test/path",
            "https://douyin.com@evil.test/path",
        ]
        for url in rejected:
            with self.subTest(url=url), self.assertRaises(ValueError):
                server._detect_platform(url)


class CategoryRulesTests(unittest.TestCase):
    def test_local_rules_have_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public = root / "categories.toml"
            local = root / "categories.local.toml"
            public.write_text('[[rule]]\nname = "public"\n', encoding="utf-8")
            self.assertEqual(ingest._resolve_rules_path(root, override=""), public)
            local.write_text('[[rule]]\nname = "local"\n', encoding="utf-8")
            self.assertEqual(ingest._resolve_rules_path(root, override=""), local)

    def test_python_310_tomli_fallback(self):
        import tomllib as standard_tomllib

        real_import = builtins.__import__

        def python_310_import(name, *args, **kwargs):
            if name == "tomllib":
                raise ModuleNotFoundError("mock Python 3.10")
            if name == "tomli":
                return standard_tomllib
            return real_import(name, *args, **kwargs)

        public = Path(ingest.__file__).with_name("categories.toml")
        with patch.object(builtins, "__import__", side_effect=python_310_import):
            self.assertTrue(ingest.load_rules(public))


if __name__ == "__main__":
    unittest.main()
