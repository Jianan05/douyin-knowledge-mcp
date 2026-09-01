import json
import tempfile
import unittest
from pathlib import Path

import favorite_audit as fa


class FavoriteAuditTests(unittest.TestCase):
    def test_duration_conflict_is_confirmed_without_reading_body_into_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "douyin_12345678901.md"
            note.write_text("---\n---\n\n## 转写稿\n\n秘密正文\n", encoding="utf-8")
            remote = [{
                "aweme_id": "12345678901", "desc": "正确标题", "kind": "video",
                "duration_ms": 27000, "url": "https://www.douyin.com/video/12345678901",
            }]
            local = {"12345678901": {
                "video_id": "12345678901", "title": "错误动漫标题", "duration": 401,
                "url": "https://www.douyin.com/video/12345678901", "path": str(note),
            }}
            findings, stats = fa.audit_rows(remote, local)
            self.assertEqual(stats["confirmed"], 1)
            self.assertEqual(findings[0].level, "confirmed")
            self.assertNotIn("秘密正文", json.dumps(findings[0].as_dict(), ensure_ascii=False))

    def test_title_only_and_liked_only_are_review_not_auto_quarantine(self):
        remote = [{
            "aweme_id": "12345678901", "desc": "苹果种植方法详解", "kind": "video",
            "duration_ms": 30000, "url": "https://www.douyin.com/video/12345678901",
        }]
        local = {
            "12345678901": {"video_id": "12345678901", "title": "宇宙飞船维修指南", "duration": 30, "path": ""},
            "22222222222": {"video_id": "22222222222", "title": "历史手动入库", "duration": 20, "path": ""},
        }
        liked = [{
            "aweme_id": "22222222222", "desc": "点赞作品", "kind": "video",
            "duration_ms": 20000, "url": "https://www.douyin.com/video/22222222222",
        }]
        findings, stats = fa.audit_rows(remote, local, liked)
        self.assertEqual(stats["confirmed"], 0)
        self.assertEqual(stats["review"], 2)

    def test_prompt_echo_is_detected_locally(self):
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "note.md"
            note.write_text(
                "---\n---\n\n## 转写稿\n\n以下是普通话的句子，请用简体中文转写。\n",
                encoding="utf-8",
            )
            self.assertTrue(fa.has_prompt_echo(note))

    def test_prompt_echo_scan_covers_local_items_not_in_current_favorites(self):
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "douyin_33333333333.md"
            note.write_text(
                "---\n---\n\n## 转写稿\n\n内容可能涉及这些术语：Agent、RAG。\n",
                encoding="utf-8",
            )
            local = {"33333333333": {
                "video_id": "33333333333", "title": "历史作品", "duration": 5,
                "url": "https://www.douyin.com/video/33333333333", "path": str(note),
            }}
            findings, stats = fa.audit_rows([], local)
            self.assertEqual(stats["confirmed"], 1)
            self.assertEqual(findings[0].aweme_id, "33333333333")

    def test_quarantine_keeps_manifest_and_index_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            note = root / "inbox" / "douyin_12345678901.md"
            note.parent.mkdir()
            note.write_text("body", encoding="utf-8")
            index = root / "index.jsonl"
            index.write_text('{"video_id":"12345678901"}\n', encoding="utf-8")
            finding = fa.Finding(
                aweme_id="12345678901", level="confirmed", reasons=["测试"], path=str(note)
            )
            target, moved = fa.quarantine_confirmed(root, [finding], index)
            self.assertEqual(moved, 1)
            self.assertFalse(note.exists())
            self.assertTrue((target / "manifest.json").is_file())
            self.assertTrue((target / "index.before.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
