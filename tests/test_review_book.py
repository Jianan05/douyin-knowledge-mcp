from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chunks
import ingest
import review_book as rb


class ReviewBookTests(unittest.TestCase):
    def _library(self, root: Path) -> None:
        inbox = root / "inbox"
        inbox.mkdir()
        rows = []
        for video_id, title in (("10000000001", "第一条"), ("10000000002", "第二条")):
            path = inbox / f"douyin_{video_id}.md"
            path.write_text(
                "\n".join([
                    "---", f'title: "{title}"',
                    f'source: "https://www.iesdouyin.com/share/video/{video_id}/?share_sign=old"',
                    f'video_id: "{video_id}"', 'tags: ["测试"]',
                    'category: ""', "platform: douyin", "---", "",
                    "## 转写稿", "", "这是一段用于审阅状态本测试的转写内容。" * 8,
                ]),
                encoding="utf-8",
            )
            rows.append({
                "video_id": video_id,
                "url": f"https://www.iesdouyin.com/share/video/{video_id}/?share_sign=old",
                "title": title,
                "tags": ["测试"],
                "path": str(path),
                "status": "raw",
                "chars": 120,
                "duration": 20,
            })
        (root / "index.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )

    def test_prepare_uses_stable_links_and_keeps_original_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._library(root)
            chosen = rb.prepare_batch(root, limit=2)

            self.assertEqual(chosen, ["10000000001", "10000000002"])
            book = (root / rb.BOOK_NAME).read_text(encoding="utf-8")
            self.assertIn("https://www.douyin.com/video/10000000001", book)
            self.assertNotIn("share_sign=old", book)
            self.assertIn("转写预览", book)
            self.assertEqual(len(list((root / "inbox").glob("*.md"))), 2)

    def test_related_notes_never_enter_preview_or_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._library(root)
            path = root / "inbox" / "douyin_10000000001.md"
            original = path.read_text(encoding="utf-8")
            path.write_text(
                original + ("这是超过旧预览上限但仍属于原始转写的内容。" * 20)
                + "\n\n## 相关笔记\n\n- [[其它作品]] — 这不是原始转录\n",
                encoding="utf-8",
            )

            rb.prepare_batch(root, limit=2)
            state = rb.load_states(root / rb.STATE_NAME)["10000000001"]
            self.assertNotIn("这不是原始转录", state["quality"]["preview"])
            self.assertGreater(len(state["quality"]["preview"]), 280)
            self.assertIn("这是超过旧预览上限但仍属于原始转写的内容。", state["quality"]["preview"])
            pieces = chunks.chunk_file(path)
            self.assertTrue(pieces)
            self.assertNotIn("这不是原始转录", "".join(piece["text"] for piece in pieces))
            self.assertIn("## 相关笔记", path.read_text(encoding="utf-8"))

    def test_exclude_is_append_only_and_filtered_from_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._library(root)
            rb.prepare_batch(root, limit=2)
            rb.set_status(root, "10000000001", "不纳入知识库", "与目标无关")

            events = (root / rb.STATE_NAME).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(events), 3)
            self.assertTrue((root / "inbox" / "douyin_10000000001.md").is_file())
            rows = chunks.chunk_library(root)
            self.assertEqual({row["video_id"] for row in rows}, {"10000000002"})
            self.assertTrue(all(row["review_status"] == "pending" for row in rows))

    def test_entering_reference_state_queues_one_impact_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._library(root)
            rb.prepare_batch(root, limit=1)

            rb.set_status(root, "10000000001", "可参考", "值得保留")
            rb.set_status(root, "10000000001", "重点深挖", "进一步查看")

            events = [
                json.loads(line)
                for line in (root / rb.IMPACT_QUEUE_NAME).read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event"], "queued")
            self.assertEqual(events[0]["video_id"], "10000000001")

    def test_delete_requires_request_and_exact_second_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._library(root)
            rb.prepare_batch(root, limit=2)
            with self.assertRaises(ValueError):
                rb.execute_delete(root, "10000000001", "10000000001")

            rb.set_status(root, "10000000001", "彻底删除", "用户要求删除")
            with self.assertRaises(ValueError):
                rb.execute_delete(root, "10000000001", "wrong")

            package = root / "_source_packages" / "10000000001.json"
            package.parent.mkdir()
            package.write_text('{"raw_text":"原始内容"}', encoding="utf-8")
            asset = root / "assets" / "10000000001" / "frame.jpg"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"image")
            calibration = root / "视觉校准" / "报告.md"
            calibration.parent.mkdir()
            calibration.write_text(
                "# 报告\n\n## 01 删除项\n- ID 10000000001\n- 提取内容\n\n"
                "## 02 保留项\n- ID 10000000002\n- 其它内容\n",
                encoding="utf-8",
            )
            fake_project = root / "project"
            cache = fake_project / "data" / "chunk_vectors.npz"
            cache.parent.mkdir(parents=True)
            cache.write_bytes(b"vectors")
            with patch.object(rb, "PROJECT", fake_project):
                result = rb.execute_delete(root, "10000000001", "10000000001")

            self.assertFalse((root / "inbox" / "douyin_10000000001.md").exists())
            self.assertTrue((root / "inbox" / "douyin_10000000002.md").is_file())
            self.assertFalse(package.exists())
            self.assertFalse(asset.parent.exists())
            calibration_text = calibration.read_text(encoding="utf-8")
            self.assertNotIn("10000000001", calibration_text)
            self.assertIn("10000000002", calibration_text)
            self.assertFalse(cache.exists())
            marker = json.loads((root / rb.TOMBSTONE_NAME).read_text(encoding="utf-8"))
            self.assertEqual(set(marker), {"schema_version", "video_id", "status", "deleted_at"})
            self.assertTrue(ingest.Library(root).is_deleted("10000000001"))
            self.assertEqual(result["removed_rows"], 3)

    def test_confirmed_mapping_mismatch_is_preserved_but_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._library(root)
            rb.prepare_batch(root, limit=2)
            source = root / "inbox" / "douyin_10000000001.md"
            before = source.read_text(encoding="utf-8")

            rb.confirm_mapping_mismatch(root, "10000000001", "原片与动画稿完全无关")

            state = rb.load_states(root / rb.STATE_NAME)["10000000001"]
            self.assertEqual(state["review_status"], "exclude")
            self.assertEqual(state["mapping_status"], "confirmed_mismatch")
            self.assertEqual(source.read_text(encoding="utf-8"), before)
            self.assertNotIn("10000000001", {row["video_id"] for row in chunks.chunk_library(root)})

    def test_random_batch_excludes_existing_and_empty_title_risks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._library(root)
            rows = rb.load_index(root / "index.jsonl")
            risky = dict(rows[1])
            risky.update({"video_id": "10000000003", "title": "", "path": rows[1]["path"]})
            with (root / "index.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(risky, ensure_ascii=False) + "\n")
            rb.prepare_batch(root, limit=1)

            chosen = rb.prepare_batch(root, limit=15, randomize=True)

            self.assertEqual(chosen, ["10000000002"])
            states = rb.load_states(root / rb.STATE_NAME)
            self.assertNotIn("10000000003", states)
            self.assertEqual(states["10000000002"]["selection_method"], "system_random")

    def test_prepare_specific_ids_preserves_requested_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._library(root)

            chosen = rb.prepare_batch(
                root,
                video_ids=["10000000002", "10000000001", "10000000002"],
            )

            self.assertEqual(chosen, ["10000000002", "10000000001"])
            states = rb.load_states(root / rb.STATE_NAME)
            self.assertEqual(states["10000000002"]["batch_order"], 1)
            self.assertEqual(states["10000000001"]["batch_order"], 2)
            self.assertTrue(all(
                states[video_id]["selection_method"] == "explicit_ids"
                for video_id in chosen
            ))

    def test_prepare_specific_ids_rejects_unknown_or_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._library(root)
            with self.assertRaisesRegex(ValueError, "索引中没有作品"):
                rb.prepare_batch(root, video_ids=["missing"])
            rb.prepare_batch(root, video_ids=["10000000001"])
            with self.assertRaisesRegex(ValueError, "已在状态本"):
                rb.prepare_batch(root, video_ids=["10000000001"])

    def test_unreadable_candidate_is_rejected_and_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._library(root)
            first = root / "inbox" / "douyin_10000000001.md"
            first.write_text("---\nvideo_id: \"10000000001\"\n---\n\n## 损坏标题\n乱码", encoding="utf-8")

            chosen = rb.prepare_batch(root, limit=1)

            self.assertEqual(chosen, ["10000000002"])
            states = rb.load_states(root / rb.STATE_NAME)
            self.assertEqual(states["10000000001"]["event"], "candidate_rejected")
            self.assertEqual(states["10000000001"]["review_status"], "exclude")


if __name__ == "__main__":
    unittest.main()
