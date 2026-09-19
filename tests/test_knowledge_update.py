from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import impact_review
import knowledge_update
import review_book
import chunks


class KnowledgeUpdateTests(unittest.TestCase):
    def _library(self, root: Path, action: str) -> tuple[Path, str]:
        note = root / "notes" / "topic.md"
        note.parent.mkdir(parents=True)
        note.write_text(
            "---\ntitle: \"主题\"\nknowledge_status: \"confirmed\"\n"
            "confirmed_by: \"用户\"\nconfirmed_at: \"2026-09-19T10:00:00+12:00\"\n"
            "source_ids: [\"old\"]\n---\n\n# 主题\n\n"
            "## 已确认结论\n\n旧结论。\n\n"
            "## 形成理由\n\n旧理由。（用户文字确认）\n\n"
            "## 适用范围\n\n旧范围。\n\n"
            "## 来源\n\n- 旧来源 — `video_id=old`\n\n"
            "## 溯源说明\n\n保留历史。\n",
            encoding="utf-8",
        )
        source = root / "inbox" / "new.md"
        source.parent.mkdir()
        source.write_text("来源", encoding="utf-8")
        (root / "index.jsonl").write_text(json.dumps({
            "video_id": "new", "title": "新来源",
            "url": "https://www.douyin.com/video/new", "path": str(source),
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        review_book.append_event(root / review_book.STATE_NAME, {
            "video_id": "new", "review_status": "reference",
        })
        candidate_id = "candidate-1"
        review_book.append_event(root / impact_review.EVENTS_NAME, {
            "event": "impact_relation_reviewed", "candidate_id": candidate_id,
            "source_id": "new", "note_path": str(note),
            "note_sha256": hashlib.sha256(note.read_bytes()).hexdigest(),
            "relation": "refines", "action": action,
        })
        return note, candidate_id

    def test_supplement_preserves_old_conclusion_and_adds_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            note, candidate_id = self._library(root, "supplement")
            knowledge_update.apply_update(
                root, candidate_id=candidate_id, update_text="新增限制。",
                rationale="新来源补足边界。", scope="只适用于测试。",
                confirmed_by="测试用户", user_confirmed=True,
            )
            text = note.read_text(encoding="utf-8")
            self.assertIn("旧结论。", text)
            self.assertIn("新增限制。（用户文字确认）", text)
            self.assertIn("### ", text)
            self.assertIn("— 补充", text)
            self.assertIn('source_ids: ["old", "new"]', text)
            self.assertIn("video_id=new", text)
            indexed_text = "".join(row["text"] for row in chunks.chunk_file(note))
            self.assertIn("旧结论。", indexed_text)
            self.assertIn("新增限制。", indexed_text)
            event = [json.loads(line) for line in (root / "_knowledge_events.jsonl").read_text(encoding="utf-8").splitlines()][0]
            self.assertEqual(event["event"], "knowledge_updated")
            with self.assertRaisesRegex(ValueError, "已经应用过"):
                knowledge_update.apply_update(
                    root, candidate_id=candidate_id, update_text="再次补充。",
                    rationale="不应重复。", scope="", confirmed_by="测试用户",
                    user_confirmed=True,
                )

    def test_supersede_keeps_old_conclusion_marked_void(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            note, candidate_id = self._library(root, "supersede")
            knowledge_update.apply_update(
                root, candidate_id=candidate_id, update_text="新结论。",
                rationale="旧条件不成立。", scope="新范围。",
                confirmed_by="测试用户", user_confirmed=True,
            )
            text = note.read_text(encoding="utf-8")
            current = knowledge_update._section(text, "已确认结论")
            self.assertIn("新结论。", current)
            self.assertNotIn("旧结论。", current)
            self.assertIn("❌ 作废的旧结论：旧结论。", text)
            self.assertIn("— 作废替代", text)
            indexed_text = "".join(row["text"] for row in chunks.chunk_file(note))
            self.assertIn("新结论。", indexed_text)
            self.assertNotIn("❌ 作废的旧结论", indexed_text)

    def test_requires_confirmation_and_rejects_stale_or_repeat(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            note, candidate_id = self._library(root, "supplement")
            kwargs = dict(
                candidate_id=candidate_id, update_text="补充。", rationale="理由。",
                scope="", confirmed_by="测试用户",
            )
            with self.assertRaisesRegex(ValueError, "缺少人工确认"):
                knowledge_update.apply_update(root, user_confirmed=False, **kwargs)
            note.write_text(note.read_text(encoding="utf-8") + "\n外部变化", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "已经变化"):
                knowledge_update.apply_update(root, user_confirmed=True, **kwargs)

    def test_new_topic_cannot_overwrite_existing_note(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, candidate_id = self._library(root, "new_topic")
            with self.assertRaisesRegex(ValueError, "knowledge_notes.py"):
                knowledge_update.apply_update(
                    root, candidate_id=candidate_id, update_text="新主题。",
                    rationale="不同主题。", scope="", confirmed_by="测试用户",
                    user_confirmed=True,
                )


if __name__ == "__main__":
    unittest.main()
