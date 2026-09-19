from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chunks
import review_book
import transcript_correction as tc


class TranscriptCorrectionTests(unittest.TestCase):
    def _library(self, root: Path) -> Path:
        inbox = root / "inbox"
        inbox.mkdir()
        source = inbox / "source_12345678901.md"
        source.write_text(
            '---\ntitle: "测试"\nsource: "https://www.douyin.com/video/12345678901"\n'
            'video_id: "12345678901"\nplatform: douyin\n---\n\n'
            "## 转写稿\n\n这是包含明显错词的原始转录内容，必须一直保留作为证据。" * 3,
            encoding="utf-8",
        )
        row = {
            "video_id": "12345678901", "title": "测试",
            "url": "https://www.douyin.com/video/12345678901",
            "path": str(source), "status": "raw", "chars": 90, "duration": 20,
            "category": "测试类", "tags": ["校正"],
        }
        (root / "index.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        review_book.prepare_batch(root, video_ids=["12345678901"])
        review_book.set_status(root, "12345678901", "needs_correction", "存在错词")
        return source

    def test_candidate_does_not_replace_original_or_enter_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._library(root)
            before = source.read_bytes()
            event = tc.create_correction(
                root, "12345678901", "这是经过原片核对后的正确转录内容。" * 4,
                basis="逐句对照原视频（强）", corrected_by="tester",
            )

            self.assertEqual(source.read_bytes(), before)
            self.assertTrue(Path(event["correction_path"]).is_file())
            text = "".join(row["text"] for row in chunks.chunk_library(root))
            self.assertIn("明显错词", text)
            self.assertNotIn("正确转录", text)

    def test_approved_candidate_replaces_only_downstream_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._library(root)
            before = source.read_bytes()
            created = tc.create_correction(
                root, "12345678901", "这是经过原片核对后的正确转录内容。" * 4,
                basis="逐句对照原视频（强）", corrected_by="tester",
            )
            tc.approve_correction(
                root, "12345678901", created["version"], review_status="reference",
                approved_by="reviewer", note="核对通过",
            )

            rows = chunks.chunk_library(root)
            text = "".join(row["text"] for row in rows)
            self.assertIn("正确转录", text)
            self.assertNotIn("明显错词", text)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(rows[0]["correction_version"], 1)
            self.assertEqual(rows[0]["original_path"], str(source))
            self.assertEqual(rows[0]["category"], "测试类")
            self.assertEqual(rows[0]["tags"], ["校正"])
            queue = [json.loads(line) for line in (root / review_book.IMPACT_QUEUE_NAME).read_text(encoding="utf-8").splitlines()]
            self.assertEqual(queue[-1]["video_id"], "12345678901")

    def test_source_change_blocks_approval_and_active_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._library(root)
            created = tc.create_correction(
                root, "12345678901", "这是经过核对后的修正版正文。" * 5,
                basis="对照来源（强）", corrected_by="tester",
            )
            source.write_text(source.read_text(encoding="utf-8") + "\n外部变化", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "原始转录已变化"):
                tc.approve_correction(
                    root, "12345678901", created["version"], review_status="reference",
                    approved_by="reviewer",
                )

    def test_revoke_restores_original_downstream_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._library(root)
            created = tc.create_correction(
                root, "12345678901", "这是经过核对后的修正版正文。" * 5,
                basis="对照来源（强）", corrected_by="tester",
            )
            tc.approve_correction(
                root, "12345678901", created["version"], review_status="deep_dive",
                approved_by="reviewer",
            )
            tc.revoke_correction(root, "12345678901", note="发现依据不足", revoked_by="reviewer")

            self.assertFalse(tc.load_active_corrections(root))
            text = "".join(row["text"] for row in chunks.chunk_library(root))
            self.assertIn("明显错词", text)
            state = review_book.load_states(root / review_book.STATE_NAME)["12345678901"]
            self.assertEqual(state["review_status"], "needs_correction")

    def test_new_approved_version_requeues_impact_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._library(root)
            first = tc.create_correction(
                root, "12345678901", "第一版校正后的完整正文。" * 6,
                basis="对照来源（强）", corrected_by="tester",
            )
            tc.approve_correction(
                root, "12345678901", first["version"], review_status="reference",
                approved_by="reviewer",
            )
            review_book.set_status(root, "12345678901", "needs_correction", "发现新错词")
            second = tc.create_correction(
                root, "12345678901", "第二版再次核对后的完整正文。" * 6,
                basis="再次对照来源（强）", corrected_by="tester",
            )
            # Simulate keeping a source in the reference pool while replacing its correction.
            review_book.set_status(root, "12345678901", "reference", "等待新版批准")
            before = len((root / review_book.IMPACT_QUEUE_NAME).read_text(encoding="utf-8").splitlines())
            tc.approve_correction(
                root, "12345678901", second["version"], review_status="reference",
                approved_by="reviewer",
            )
            after = len((root / review_book.IMPACT_QUEUE_NAME).read_text(encoding="utf-8").splitlines())
            self.assertEqual(after, before + 1)


if __name__ == "__main__":
    unittest.main()
