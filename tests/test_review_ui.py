import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import review_book
import review_ui


class ReviewUiTests(unittest.TestCase):
    def test_page_has_required_controls_and_no_delete_api(self):
        for label in ("可参考", "重点深挖", "转录需修正", "不纳入知识库", "申请删除"):
            self.assertIn(label, review_ui.PAGE)
        self.assertNotIn("只保存备注", review_ui.PAGE)
        self.assertNotIn("data-note-only", review_ui.PAGE)
        self.assertIn("尝试独立打开原片", review_ui.PAGE)
        self.assertIn("window.open", review_ui.PAGE)
        self.assertNotIn("<iframe", review_ui.PAGE)
        self.assertIn("尝试 Chrome 左右并排", review_ui.PAGE)
        self.assertIn("Win+左", review_ui.PAGE)
        self.assertIn("清洗后短预览", review_ui.PAGE)
        self.assertIn("展开/收起清洗后全文", review_ui.PAGE)
        self.assertIn("查看原始 OCR/转写全文", review_ui.PAGE)
        self.assertIn("内容类型", review_ui.PAGE)
        for label in ("审阅状态", "视频类型", "常用判断", "自由备注", "初始候选"):
            self.assertIn(label, review_ui.PAGE)
        self.assertIn("/api/annotations", review_ui.PAGE)
        self.assertNotIn("/api/delete", review_ui.PAGE)

    def test_annotations_restore_cancel_and_do_not_overwrite_status(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            review_book.append_event(root / review_book.STATE_NAME, {
                "event": "prepared", "video_id": "tags-1", "review_status": "deep_dive",
                "review_batch_id": "batch", "prepared_at": "2026-09-02T00:00:00+12:00",
                "batch_order": 1, "human_note": "原备注",
            })
            first = review_ui.save_annotations(root, {
                "video_id": "tags-1", "video_types": ["纯口播", "营销号"],
            })
            self.assertEqual(first["review_status"], "deep_dive")
            self.assertEqual(first["video_types"], ["纯口播", "营销号"])
            self.assertEqual(first["human_note"], "原备注")

            cancelled = review_ui.save_annotations(root, {
                "video_id": "tags-1", "video_types": ["纯口播"],
            })
            self.assertEqual(cancelled["video_types"], ["纯口播"])
            phrase = "高互动量，研究传播/剪辑手法"
            review_ui.save_annotations(root, {"video_id": "tags-1", "append_judgment": phrase})
            final = review_ui.save_annotations(root, {"video_id": "tags-1", "append_judgment": phrase})
            self.assertEqual(final["human_note"].count(phrase), 1)
            self.assertEqual(final["review_status"], "deep_dive")
            restored = review_ui.first_batch(root)[0]
            self.assertEqual(restored["video_types"], ["纯口播"])
            self.assertEqual(restored["human_note"].count(phrase), 1)

            clip = review_ui.save_annotations(root, {
                "video_id": "tags-1", "append_judgment": "观点视频的嵌入素材音频漏转",
            })
            self.assertEqual(clip["review_status"], "deep_dive")
            state = review_book.load_states(root / review_book.STATE_NAME)["tags-1"]
            self.assertEqual(state["reference_clip_segment"]["status"], "awaiting_media_review")
            self.assertIsNone(state["reference_clip_segment"]["time_range"])
            self.assertIsNone(state["reference_clip_segment"]["transcript_candidate"])

    def test_each_status_and_note_append_without_deleting_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "inbox" / "sample.md"
            source.parent.mkdir()
            source.write_text("原始内容", encoding="utf-8")
            review_book.append_event(root / review_book.STATE_NAME, {
                "schema_version": 1, "event": "prepared", "video_id": "123",
                "review_status": "pending", "human_note": "",
                "prepared_at": "2026-09-02T00:00:00+12:00", "batch_order": 1,
                "title": "样本", "url": "https://www.douyin.com/video/123",
                "tags": ["测试"], "path": str(source),
                "quality": {"preview": "转录预览", "verification": "待核验", "flags": []},
            })
            before = source.read_text(encoding="utf-8")
            statuses = ["reference", "deep_dive", "needs_correction", "exclude", "delete_requested"]
            for index, status in enumerate(statuses):
                result = review_ui.save_review(root, {"video_id": "123", "status": status, "note": f"备注{index}"})
                self.assertEqual(result, {"video_id": "123", "review_status": status, "human_note": f"备注{index}"})
                self.assertEqual(source.read_text(encoding="utf-8"), before)
                self.assertFalse((root / review_book.TOMBSTONE_NAME).exists())
            lines = (root / review_book.STATE_NAME).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1 + len(statuses))
            self.assertTrue(all(json.loads(line)["video_id"] == "123" for line in lines))

    def test_http_button_endpoint_appends_status(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            review_book.append_event(root / review_book.STATE_NAME, {
                "event": "prepared", "video_id": "456", "review_status": "pending",
                "prepared_at": "2026-09-02T00:00:00+12:00", "batch_order": 1,
                "url": "https://www.douyin.com/video/456", "quality": {"preview": "预览"},
            })
            server = ThreadingHTTPServer(("127.0.0.1", 0), review_ui.make_handler(root))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                body = json.dumps({"video_id": "456", "status": "reference", "note": "网页备注"}).encode("utf-8")
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/api/status", data=body,
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                with urllib.request.urlopen(request) as response:
                    result = json.loads(response.read().decode("utf-8"))
                self.assertEqual(result["review_status"], "reference")
                state = review_book.load_states(root / review_book.STATE_NAME)["456"]
                self.assertEqual(state["human_note"], "网页备注")
                self.assertFalse((root / review_book.TOMBSTONE_NAME).exists())
            finally:
                server.shutdown()
                server.server_close()

    def test_newest_named_batch_is_displayed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for video_id, batch_id, prepared_at in (
                ("old", "batch-old", "2026-09-01T00:00:00+12:00"),
                ("new", "batch-new", "2026-09-02T00:00:00+12:00"),
            ):
                review_book.append_event(root / review_book.STATE_NAME, {
                    "event": "prepared", "video_id": video_id, "review_status": "pending",
                    "review_batch_id": batch_id, "prepared_at": prepared_at, "batch_order": 1,
                })
            self.assertEqual([row["video_id"] for row in review_ui.first_batch(root)], ["new"])


if __name__ == "__main__":
    unittest.main()
