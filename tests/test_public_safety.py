from __future__ import annotations

import asyncio
import builtins
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import douyin_collects as dc
import image_note
import ingest
import server


PLACEHOLDER_URL = "https://www.douyin.com/video/1234567890123456789"


def favorite_args(**overrides):
    values = {
        "dry_run": False,
        "brief": True,
        "force": False,
        "no_screen": False,
        "keep_collected": False,
        "force_uncollect": False,
        "max_items": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def favorite(aid: str, kind: str = "video") -> dict:
    return {
        "aweme_id": aid,
        "kind": kind,
        "desc": "占位测试内容",
        "duration_ms": 1000,
        "url": PLACEHOLDER_URL,
    }


class EmptyContentSafetyTests(unittest.TestCase):
    def _run_empty_video(self, screen_result=None, screen_error=None) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            lib = ingest.Library(Path(tmp))

            async def fake_download(_url, out_dir, on_progress=None):
                return str(Path(out_dir) / "placeholder.m4a"), "douyin"

            screen_mock = AsyncMock(return_value=screen_result)
            if screen_error is not None:
                screen_mock.side_effect = screen_error
            with patch.object(server, "_detect_platform", return_value="douyin"), patch.object(
                ingest, "_resolve_video_id", return_value=(PLACEHOLDER_URL, "placeholder")
            ), patch.object(
                server, "_download_transcription_media", side_effect=fake_download
            ), patch.object(
                server, "capture_meta", return_value={"title": "占位标题", "video_id": "placeholder"}
            ), patch.object(
                server, "_transcribe_segments_sync", return_value=""
            ), patch.object(
                ingest, "_media_duration", return_value=10.0
            ), patch.object(
                ingest, "read_screen_text", screen_mock
            ), patch.object(server, "device_label", return_value="CPU"):
                return asyncio.run(ingest.ingest_one(PLACEHOLDER_URL, lib, "tiny", False))

    def test_no_speech_and_no_valid_ocr_is_warning(self):
        line = self._run_empty_video(screen_result=("", []))
        self.assertTrue(line.startswith("[warn]"))
        self.assertIn("无语音且无有效 OCR", line)

    def test_ocr_exception_is_warning(self):
        line = self._run_empty_video(screen_error=RuntimeError("mock OCR error"))
        self.assertTrue(line.startswith("[warn]"))
        self.assertIn("OCR 失败", line)

    def test_partial_image_ocr_failure_is_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = ingest.Library(Path(tmp))
            item = favorite("image-placeholder", "image")
            item["images"] = ["https://example.invalid/a", "https://example.invalid/b"]
            with patch.object(
                image_note,
                "ocr_images",
                return_value=("### 图 1\n\n有效占位文字\n\n### 图 2\n\n（识别失败：RuntimeError）", 1),
            ):
                line = asyncio.run(ingest.ingest_image_post(item, lib, False))
            self.assertTrue(line.startswith("[warn]"))
            self.assertIn("OCR 仅完成 1/2 张", line)


class FavoriteUncollectSafetyTests(unittest.TestCase):
    def test_index_row_without_existing_file_is_not_known_or_queued(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = ingest.Library(Path(tmp))
            lib.record({"video_id": "stale", "path": str(Path(tmp) / "missing.md")})
            self.assertIsNone(lib.known("stale"))
            process = AsyncMock(return_value="[skip] stale | 索引声称已入库")
            run_pending = AsyncMock()
            with patch.object(
                dc, "fetch_favorites", AsyncMock(return_value=({"complete": True}, [favorite("stale")]))
            ), patch.object(ingest, "_ingest_collected_item", process), patch.object(
                ingest, "_run_pending_uncollect", run_pending
            ):
                asyncio.run(ingest.cmd_favorites(favorite_args(), lib, "tiny"))
            self.assertEqual(lib.pending_uncollect(), [])
            run_pending.assert_not_awaited()

    def test_existing_indexed_note_is_safe_skip_and_can_be_queued(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = ingest.Library(Path(tmp))
            note = Path(tmp) / "inbox" / "placeholder.md"
            note.write_text("placeholder", encoding="utf-8")
            lib.record({"video_id": "saved", "path": str(note)})
            run_pending = AsyncMock(return_value=0)
            process = AsyncMock(return_value="[skip] saved | 已安全入库")
            with patch.object(
                dc, "fetch_favorites", AsyncMock(return_value=({"complete": True}, [favorite("saved")]))
            ), patch.object(ingest, "_ingest_collected_item", process), patch.object(
                ingest, "_run_pending_uncollect", run_pending
            ):
                asyncio.run(ingest.cmd_favorites(favorite_args(), lib, "tiny"))
            self.assertEqual([row["aweme_id"] for row in lib.pending_uncollect()], ["saved"])
            run_pending.assert_awaited_once()

    def test_warn_fail_and_unknown_are_never_queued(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = ingest.Library(Path(tmp))
            items = [favorite("warn"), favorite("fail"), favorite("unknown", "unknown")]
            outcomes = {
                "warn": "[warn] warn | 视觉价值不确定",
                "fail": "[fail] fail | OCR 失败",
                "unknown": "[fail] unknown | 未识别的作品类型",
            }

            async def process(item, *_args, **_kwargs):
                return outcomes[item["aweme_id"]]

            run_pending = AsyncMock()
            with patch.object(
                dc, "fetch_favorites", AsyncMock(return_value=({"complete": True}, items))
            ), patch.object(ingest, "_ingest_collected_item", side_effect=process), patch.object(
                ingest, "_FAVORITES_ITEM_RETRY_DELAYS", ()
            ), patch.object(ingest, "_run_pending_uncollect", run_pending):
                asyncio.run(ingest.cmd_favorites(favorite_args(), lib, "tiny"))
            self.assertEqual(lib.pending_uncollect(), [])
            run_pending.assert_not_awaited()

    def test_incomplete_inventory_never_queues_or_uncollects(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = ingest.Library(Path(tmp))
            note = Path(tmp) / "inbox" / "placeholder.md"
            note.write_text("placeholder", encoding="utf-8")
            lib.record({"video_id": "saved", "path": str(note)})
            fetch = AsyncMock(return_value=({"complete": False}, [favorite("saved")]))
            run_pending = AsyncMock()
            process = AsyncMock(return_value="[skip] saved | 已安全入库")
            with patch.object(dc, "fetch_favorites", fetch), patch.object(
                ingest, "_ingest_collected_item", process
            ), patch.object(ingest, "_run_pending_uncollect", run_pending
            ):
                asyncio.run(ingest.cmd_favorites(favorite_args(), lib, "tiny"))
            fetch.assert_awaited_once_with(require_exhausted=True)
            self.assertEqual(lib.pending_uncollect(), [])
            run_pending.assert_not_awaited()

    def test_keep_collected_never_writes_or_runs_uncollect_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = ingest.Library(Path(tmp))
            note = Path(tmp) / "inbox" / "placeholder.md"
            note.write_text("placeholder", encoding="utf-8")
            lib.record({"video_id": "saved", "path": str(note)})
            run_pending = AsyncMock()
            process = AsyncMock(return_value="[skip] saved | 已安全入库")
            with patch.object(
                dc, "fetch_favorites", AsyncMock(return_value=({"complete": True}, [favorite("saved")]))
            ), patch.object(ingest, "_ingest_collected_item", process), patch.object(
                ingest, "_run_pending_uncollect", run_pending
            ):
                asyncio.run(ingest.cmd_favorites(favorite_args(keep_collected=True), lib, "tiny"))
            self.assertEqual(lib.pending_uncollect(), [])
            run_pending.assert_not_awaited()

    def test_pending_uncollect_recounts_then_calls_mock_uncollect(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = ingest.Library(Path(tmp))
            lib.queue_uncollect([favorite("queued")])
            fetch = AsyncMock(return_value=({"complete": True}, [favorite("queued")]))
            uncollect = AsyncMock(return_value=(["queued"], []))
            with patch.object(dc, "fetch_favorites", fetch), patch.object(
                dc, "uncollect_items", uncollect
            ):
                code = asyncio.run(ingest._run_pending_uncollect(lib, force_now=True))
            self.assertEqual(code, 0)
            fetch.assert_awaited_once_with(allow_empty=True)
            uncollect.assert_awaited_once()
            self.assertEqual(lib.pending_uncollect(), [])

    def test_pending_uncollect_stops_when_recount_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = ingest.Library(Path(tmp))
            lib.queue_uncollect([favorite("queued")])
            uncollect = AsyncMock()
            with patch.object(
                dc, "fetch_favorites", AsyncMock(return_value=({"complete": False}, [favorite("queued")]))
            ), patch.object(dc, "uncollect_items", uncollect):
                code = asyncio.run(ingest._run_pending_uncollect(lib, force_now=True))
            self.assertEqual(code, 1)
            uncollect.assert_not_awaited()
            self.assertEqual([row["aweme_id"] for row in lib.pending_uncollect()], ["queued"])


class PlatformDetectionTests(unittest.TestCase):
    def test_allows_only_root_domains_and_real_subdomains(self):
        cases = {
            "https://douyin.com/path": "douyin",
            "https://WWW.DOUYIN.COM:443/path": "douyin",
            "https://a.b.iesdouyin.com/path": "douyin",
            "https://bilibili.com/video/placeholder": "bilibili",
            "https://www.bilibili.com:8443/video/placeholder": "bilibili",
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
    def test_public_categories_toml_loads(self):
        rules = ingest.load_rules(Path(ingest.__file__).with_name("categories.toml"))
        self.assertTrue(rules)
        self.assertTrue(all("name" in rule for rule in rules))

    def test_local_categories_keep_priority_over_public_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public = root / "categories.toml"
            local = root / "categories.local.toml"
            public.write_text('[[rule]]\nname = "public"\n', encoding="utf-8")
            self.assertEqual(ingest._resolve_rules_path(root, override=""), public)
            local.write_text('[[rule]]\nname = "local"\n', encoding="utf-8")
            self.assertEqual(ingest._resolve_rules_path(root, override=""), local)
            self.assertEqual(ingest.load_rules(local)[0]["name"], "local")

    def test_python_310_tomli_fallback_loads_rules(self):
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
            rules = ingest.load_rules(public)
        self.assertTrue(rules)


if __name__ == "__main__":
    unittest.main()
