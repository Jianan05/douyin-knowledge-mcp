from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import douyin_collects as dc
import douyin_browser as db
import ingest
import visual_assets as va


class NormalizeFavoriteTests(unittest.TestCase):
    def test_target_aweme_lookup_ignores_preloaded_recommendation(self):
        payload = {
            "recommend": {"aweme_id": "wrong", "desc": "动漫", "video": {"duration": 9}},
            "aweme_detail": {
                "aweme_id": "wanted", "desc": "真正收藏", "video": {"duration": 12}
            },
        }

        found = db._find_aweme_by_id(payload, "wanted")

        self.assertEqual(found["desc"], "真正收藏")
        self.assertEqual(found["video"]["duration"], 12)

    def test_signed_share_url_is_replaced_with_stable_aweme_url(self):
        row = dc._normalize({
            "aweme_id": "123456789",
            "desc": "视频",
            "video": {"duration": 1200},
            "share_info": {
                "share_url": "https://www.iesdouyin.com/share/video/123456789/?share_sign=expired"
            },
        })

        self.assertEqual(row["url"], "https://www.douyin.com/video/123456789")

    def test_video_image_text_and_unknown_are_distinct(self):
        video = dc._normalize({
            "aweme_id": "1", "desc": "视频", "video": {"duration": 1200}
        })
        image = dc._normalize({
            "aweme_id": "2", "desc": "图文", "aweme_type": 68,
            "images": [{"url_list": ["small", "large"]}],
        })
        text = dc._normalize({"aweme_id": "3", "desc": "一篇纯文字帖子"})
        unknown = dc._normalize({"aweme_id": "4", "desc": ""})

        self.assertEqual(video["kind"], "video")
        self.assertEqual(video["_video"]["duration"], 1200)
        self.assertEqual(image["kind"], "image")
        self.assertEqual(image["images"], ["large"])
        self.assertEqual(text["kind"], "text")
        self.assertIn("/note/3", text["url"])
        self.assertEqual(unknown["kind"], "unknown")

    def test_global_feed_response_is_not_mixed_with_folder_bucket(self):
        class Response:
            def __init__(self, url, body):
                self.url = url
                self._body = body

            async def json(self):
                return self._body

        collector = dc._Collector()
        asyncio.run(collector._on_response(Response(
            "https://www.douyin.com/aweme/v1/web/aweme/listcollection/?cursor=0",
            {"aweme_list": [{
                "aweme_id": "all", "desc": "纯文字", "is_ads": True,
                "collect_stat": 0, "author": {"uid": "author-1"},
            }]},
        )))
        asyncio.run(collector._on_response(Response(
            "https://www.douyin.com/aweme/v1/web/collects/video/list/?collects_id=folder",
            {"aweme_list": [{"aweme_id": "folder-item", "desc": "夹内作品"}]},
        )))

        self.assertTrue(collector.saw_all_feed)
        self.assertEqual(set(collector.bucket(dc.ALL_FAVORITES)), {"all"})
        self.assertEqual(set(collector.bucket("folder")), {"folder-item"})
        row = collector.bucket(dc.ALL_FAVORITES)["all"]
        self.assertEqual(row["_source_path"], "/aweme/v1/web/aweme/listcollection/")
        self.assertTrue(row["_is_ads"])
        self.assertEqual(row["_collect_stat"], 0)
        self.assertEqual(row["_author_uid"], "author-1")

    def test_liked_feed_is_never_treated_as_collected(self):
        class Response:
            url = "https://www.douyin.com/aweme/v1/web/aweme/favorite/?cursor=0"

            async def json(self):
                return {
                    "aweme_list": [{
                        "aweme_id": "liked-only",
                        "desc": "只点赞、没收藏",
                        "collect_stat": 0,
                        "user_digged": 1,
                    }]
                }

        collector = dc._Collector()
        asyncio.run(collector._on_response(Response()))
        self.assertFalse(collector.saw_all_feed)
        self.assertEqual(collector.bucket(dc.ALL_FAVORITES), {})
        self.assertEqual(set(collector.ignored_liked), {"liked-only"})


class LibraryQueueTests(unittest.TestCase):
    def test_queue_is_deduplicated_and_can_be_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = ingest.Library(Path(tmp))
            lib.queue_uncollect([
                {"aweme_id": "1", "url": "https://example/1"},
                {"aweme_id": "1", "url": "https://example/new"},
                {"aweme_id": "2", "url": "https://example/2"},
            ])
            rows = {r["aweme_id"]: r for r in lib.pending_uncollect()}
            self.assertEqual(set(rows), {"1", "2"})
            self.assertEqual(rows["1"]["url"], "https://example/new")

            lib.save_pending_uncollect([])
            self.assertFalse(lib.pending_uncollect_path.exists())


class TranscriptionProgressTests(unittest.TestCase):
    def test_progress_lock_falls_back_without_interrupting_transcription(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "_转录进度.md"
            path.write_text("旧状态", encoding="utf-8")
            with patch.object(Path, "replace", side_effect=PermissionError("占用中")):
                ingest._write_progress_file(path, "新状态")
            self.assertEqual(path.read_text(encoding="utf-8"), "新状态")
            self.assertFalse(path.with_suffix(".tmp").exists())

    def test_library_creates_idle_progress_file_without_overwriting_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = ingest.Library(Path(tmp))
            self.assertIn("状态：**未启动**", lib.progress_path.read_text(encoding="utf-8"))
            lib.progress_path.write_text("保留历史状态", encoding="utf-8")
            ingest.Library(Path(tmp))
            self.assertEqual(lib.progress_path.read_text(encoding="utf-8"), "保留历史状态")

    def test_preflight_status_is_visible_before_inventory_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = ingest.Library(Path(tmp))
            lib.write_progress_status("运行中", "清点全部收藏")
            text = lib.progress_path.read_text(encoding="utf-8")
            self.assertIn("状态：**运行中**", text)
            self.assertIn("当前阶段：清点全部收藏", text)
            self.assertFalse(lib.progress_path.with_suffix(".tmp").exists())

    def test_progress_file_tracks_current_stage_and_finishes_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = ingest.Library(Path(tmp))
            progress = ingest.TranscriptionProgress(
                lib,
                mode="全部收藏（只保存，不取消收藏）",
                total=12,
                pending=4,
                eta_low_s=120,
                eta_high_s=240,
            )
            item_progress = progress.begin_item(9, {
                "aweme_id": "123",
                "desc": "一条测试视频 #AI",
            }, "检查去重")
            item_progress.stage(
                "Whisper 语音识别",
                percent=0.5,
                detail="1分 0秒/2分 0秒",
                force=True,
            )

            running = progress.path.read_text(encoding="utf-8")
            self.assertIn("状态：**运行中**", running)
            self.assertIn("当前进度：9/12", running)
            self.assertIn("123", running)
            self.assertIn("Whisper 语音识别 50%", running)
            self.assertIn("并行处理中：1 条", running)
            self.assertFalse(progress.path.with_suffix(".tmp").exists())

            progress.record("[ok] 123 | 已保存", item_progress.key)
            progress.finish("完成")
            finished = progress.path.read_text(encoding="utf-8")
            self.assertIn("状态：**完成**", finished)
            self.assertIn("已完成：1 条", finished)
            self.assertIn("当前作品：无", finished)

    def test_failure_list_contains_summary_but_progress_has_no_transcript_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = ingest.Library(Path(tmp))
            progress = ingest.TranscriptionProgress(
                lib,
                mode="测试",
                total=1,
                pending=1,
                eta_low_s=60,
                eta_high_s=120,
            )
            item_progress = progress.begin_item(
                1,
                {
                    "aweme_id": "bad",
                    "desc": "坏链接",
                    "kind": "video",
                    "url": "https://www.douyin.com/video/bad",
                },
                "下载音频",
            )
            progress.record("[fail] bad | 下载失败", item_progress.key)
            progress.finish("完成（1 条失败）")

            text = progress.path.read_text(encoding="utf-8")
            self.assertIn("失败：1 条", text)
            self.assertIn("坏链接 (`bad`) — 下载失败", text)
            self.assertIn(
                "[在抖音打开原作品](https://www.douyin.com/video/bad)", text
            )
            self.assertIn("收藏：已保留", text)
            self.assertNotIn("转录正文", text.replace("不包含任何转录正文", ""))

            permanent = lib.failure_report_path.read_text(encoding="utf-8")
            self.assertIn("待重试：1 条", permanent)
            self.assertIn("坏链接", permanent)
            self.assertIn("https://www.douyin.com/video/bad", permanent)

            retry = progress.begin_item(
                2,
                {
                    "aweme_id": "bad",
                    "desc": "坏链接",
                    "kind": "video",
                    "url": "https://www.douyin.com/video/bad",
                },
                "重试",
            )
            progress.record("[ok] bad | 已成功入库", retry.key)
            resolved = lib.failure_report_path.read_text(encoding="utf-8")
            self.assertIn("待重试：0 条", resolved)
            self.assertIn("已解决：1 条", resolved)
            self.assertIn("✅ 已解决", resolved)

    def test_keep_collected_allows_visible_partial_inventory_without_uncollect(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = ingest.Library(Path(tmp))
            args = SimpleNamespace(
                dry_run=False,
                brief=True,
                force=False,
                no_screen=True,
                keep_collected=True,
                force_uncollect=False,
            )
            fetch = AsyncMock(return_value=(
                {"complete": False},
                [{
                    "aweme_id": "visible",
                    "kind": "text",
                    "desc": "当前可见内容",
                    "duration_ms": 0,
                    "url": "https://example/visible",
                }],
            ))
            process = AsyncMock(return_value="[skip] visible | 已入库")
            with patch.object(dc, "fetch_favorites", fetch), patch.object(
                ingest, "_ingest_collected_item", process
            ):
                code = asyncio.run(ingest.cmd_favorites(args, lib, "small"))

            self.assertEqual(code, 0)
            fetch.assert_awaited_once_with(require_exhausted=False)
            self.assertFalse(lib.pending_uncollect_path.exists())
            status = lib.progress_path.read_text(encoding="utf-8")
            self.assertIn("清点未到底；只保存、不取消收藏", status)

    def test_favorites_pipeline_runs_up_to_four_items_concurrently(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = ingest.Library(Path(tmp))
            args = SimpleNamespace(
                dry_run=False,
                brief=True,
                force=False,
                no_screen=True,
                keep_collected=True,
                force_uncollect=False,
            )
            items = [
                {
                    "aweme_id": str(i),
                    "kind": "video",
                    "desc": f"视频{i}",
                    "duration_ms": 1000,
                    "url": f"https://example/{i}",
                }
                for i in range(8)
            ]
            active = 0
            maximum = 0

            async def fake_process(*_args, **_kwargs):
                nonlocal active, maximum
                active += 1
                maximum = max(maximum, active)
                await asyncio.sleep(0.02)
                active -= 1
                return "[skip] fake | 已入库"

            with patch.object(
                dc,
                "fetch_favorites",
                AsyncMock(return_value=({"complete": True}, items)),
            ), patch.object(ingest, "_ingest_collected_item", side_effect=fake_process):
                code = asyncio.run(ingest.cmd_favorites(args, lib, "small"))

            self.assertEqual(code, 0)
            self.assertEqual(maximum, ingest._FAVORITES_PIPELINE_WORKERS)

    def test_failed_item_retries_immediately_before_final_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = ingest.Library(Path(tmp))
            args = SimpleNamespace(
                dry_run=False,
                brief=True,
                force=False,
                no_screen=True,
                keep_collected=True,
                force_uncollect=False,
            )
            item = {
                "aweme_id": "retry-me",
                "kind": "video",
                "desc": "需要重试的视频",
                "duration_ms": 1000,
                "url": "https://example/retry-me",
            }
            process = AsyncMock(side_effect=[
                "[fail] retry-me | 下载失败",
                "[fail] retry-me | 页面捕获失败",
                "[ok] retry-me | 已成功入库",
            ])

            with patch.object(
                dc,
                "fetch_favorites",
                AsyncMock(return_value=({"complete": True}, [item])),
            ), patch.object(
                ingest, "_ingest_collected_item", process
            ), patch.object(
                ingest, "_FAVORITES_ITEM_RETRY_DELAYS", (0, 0)
            ):
                code = asyncio.run(ingest.cmd_favorites(args, lib, "small"))

            self.assertEqual(code, 0)
            self.assertEqual(process.await_count, 3)
            report = lib.failure_report_path.read_text(encoding="utf-8")
            self.assertIn("待重试：0 条", report)

    def test_next_download_overlaps_previous_single_gpu_transcription(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = ingest.Library(Path(tmp))
            events: list[str] = []

            async def fake_download(url, out_dir, on_progress=None):
                aid = url.rsplit("/", 1)[-1]
                events.append(f"download_start:{aid}")
                await asyncio.sleep(0.02)
                events.append(f"download_end:{aid}")
                return str(Path(out_dir) / f"{aid}.m4a"), "douyin"

            def fake_transcribe(path, model, on_segment=None):
                aid = Path(path).stem
                events.append(f"transcribe_start:{aid}")
                time.sleep(0.08)
                events.append(f"transcribe_end:{aid}")
                return "这是一段足够长的测试转录文字，不会触发画面识别兜底。"

            def identity(url):
                return url, url.rsplit("/", 1)[-1]

            def metadata(path):
                aid = Path(path).stem
                return {"title": f"标题{aid}", "video_id": aid}

            async def run_two():
                lock = asyncio.Lock()
                return await asyncio.gather(
                    ingest.ingest_one(
                        "https://example/a", lib, "medium", False, True,
                        download_lock=lock,
                    ),
                    ingest.ingest_one(
                        "https://example/b", lib, "medium", False, True,
                        download_lock=lock,
                    ),
                )

            with patch.object(
                ingest.server, "_detect_platform", return_value="douyin"
            ), patch.object(ingest, "_resolve_video_id", side_effect=identity), patch.object(
                ingest.server, "_download_transcription_media", side_effect=fake_download
            ), patch.object(
                ingest.server, "_transcribe_segments_sync", side_effect=fake_transcribe
            ), patch.object(
                ingest.server, "capture_meta", side_effect=metadata
            ), patch.object(ingest, "_media_duration", return_value=30.0):
                lines = asyncio.run(run_two())

            self.assertTrue(all(line.startswith("[ok]") for line in lines))
            first_start = events.index("transcribe_start:a")
            first_end = events.index("transcribe_end:a")
            second_download_end = events.index("download_end:b")
            self.assertLess(first_start, second_download_end)
            self.assertLess(second_download_end, first_end)


class TextPostTests(unittest.TestCase):
    def test_user_confirmed_text_override_bypasses_phantom_video_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "_内容类型修正.jsonl").write_text(
                json.dumps(
                    {"aweme_id": "99", "kind": "text", "source": "用户口头确认"},
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            lib = ingest.Library(root)
            corrected = lib.apply_type_override(
                {
                    "aweme_id": "99",
                    "kind": "video",
                    "duration_ms": 60000,
                    "desc": "正文",
                }
            )
            self.assertEqual(corrected["kind"], "text")
            self.assertEqual(corrected["duration_ms"], 0)
            self.assertEqual(corrected["type_override_source"], "用户口头确认")

    def test_text_post_is_saved_without_whisper_or_ocr(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = ingest.Library(Path(tmp))
            line = asyncio.run(ingest.ingest_text_post({
                "aweme_id": "99",
                "url": "https://www.douyin.com/note/99",
                "desc": "这是正文第一句。\n这是正文第二句。 #测试",
                "kind": "text",
            }, lib, False))
            self.assertTrue(line.startswith("[ok] 99"))
            row = lib.known("99")
            self.assertEqual(row["platform"], "douyin-文字")
            saved = Path(row["path"]).read_text(encoding="utf-8")
            self.assertIn("model: 原生文字", saved)
            self.assertIn("这是正文第二句", saved)

    def test_inventory_excludes_known_items_from_eta(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = ingest.Library(Path(tmp))
            note = Path(tmp) / "inbox" / "known.md"
            note.write_text("placeholder", encoding="utf-8")
            lib.record({"video_id": "known", "url": "x", "path": str(note)})
            inv = ingest._favorite_inventory([
                {"aweme_id": "known", "kind": "video", "duration_ms": 999999},
                {"aweme_id": "new", "kind": "video", "duration_ms": 60000},
                {"aweme_id": "txt", "kind": "text", "duration_ms": 0},
            ], lib)
            self.assertEqual(inv["known"], 1)
            self.assertEqual(len(inv["pending"]), 2)
            self.assertEqual(inv["duration_s"], 60)


class VisualClassificationTests(unittest.TestCase):
    def test_spoken_visual_and_tutorial_signals_route_differently(self):
        spoken = va.classify(
            {"desc": "关于职场认知的经验分析"},
            {"speech_chars_per_min": 300, "scene_changes_per_min": 1,
             "ocr_chars": 10, "ocr_text": "", "ui_keyword_hits": []},
        )
        visual = va.classify(
            {"desc": "AI生成视觉短片，电影感运镜"},
            {"speech_chars_per_min": 20, "scene_changes_per_min": 18,
             "ocr_chars": 0, "ocr_text": "", "ui_keyword_hits": []},
        )
        tutorial = va.classify(
            {"desc": "剪映关键帧转场教程"},
            {"speech_chars_per_min": 180, "scene_changes_per_min": 3,
             "ocr_chars": 200, "ocr_text": "时间轴 图层 导出",
             "ui_keyword_hits": ["时间轴", "图层", "导出"]},
        )

        self.assertEqual(spoken["primary_value"], "spoken_knowledge")
        self.assertEqual(visual["primary_value"], "visual_reference")
        self.assertEqual(tutorial["primary_value"], "editing_tutorial")
        self.assertIn("推断，未证实", tutorial["classification_source"])

    def test_calibration_selection_is_stratified_and_excludes_known(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = ingest.Library(Path(tmp))
            note = Path(tmp) / "inbox" / "known.md"
            note.write_text("placeholder", encoding="utf-8")
            lib.record({"video_id": "known", "url": "x", "path": str(note)})
            items = [
                {"aweme_id": "known", "kind": "video", "desc": "AI视频", "duration_ms": 1},
                {"aweme_id": "img", "kind": "image", "desc": "图文", "duration_ms": 0},
                {"aweme_id": "vis", "kind": "video", "desc": "AI生成视觉运镜", "duration_ms": 20},
                {"aweme_id": "tut", "kind": "video", "desc": "剪映转场教程", "duration_ms": 30},
                {"aweme_id": "talk", "kind": "video", "desc": "经验分析", "duration_ms": 40},
                {"aweme_id": "other", "kind": "video", "desc": "随手拍", "duration_ms": 50},
            ]
            selected = ingest._select_visual_calibration(items, lib, 5)
            ids = {i["aweme_id"] for i in selected}
            self.assertEqual(ids, {"img", "vis", "tut", "talk", "other"})
            self.assertNotIn("known", ids)


if __name__ == "__main__":
    unittest.main()
