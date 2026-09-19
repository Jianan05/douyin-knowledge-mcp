from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import knowledge_notes as kn
import review_book
import chunks


class KnowledgeNotesTests(unittest.TestCase):
    def _library(self, root: Path, state: str = "reference") -> None:
        source = root / "inbox" / "source.md"
        source.parent.mkdir(parents=True)
        source.write_text("## 转写稿\n\n不应被复制进正式笔记的原始正文。\n", encoding="utf-8")
        row = {
            "video_id": "123",
            "title": "公开测试来源",
            "url": "https://www.douyin.com/video/123",
            "path": str(source),
        }
        (root / "index.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        review_book.append_event(root / review_book.STATE_NAME, {
            "event": "reviewed",
            "video_id": "123",
            "review_status": state,
        })

    def test_promote_requires_explicit_confirmation_and_reviewed_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._library(root, "pending")
            kwargs = dict(
                title="测试结论",
                conclusion="这是人工确认的结论。",
                source_ids=["123"],
                rationale="原始材料与讨论支持",
                scope="仅用于公开演示",
                confirmed_by="测试用户",
            )
            with self.assertRaisesRegex(ValueError, "缺少人工确认"):
                kn.promote(root, user_confirmed=False, **kwargs)
            with self.assertRaisesRegex(ValueError, "只有可参考或重点深挖"):
                kn.promote(root, user_confirmed=True, **kwargs)

    def test_promote_writes_traceable_note_without_copying_transcript(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._library(root)
            path = kn.promote(
                root,
                title="测试结论",
                conclusion="这是人工确认的结论。",
                source_ids=["123", "123"],
                rationale="原始材料与讨论支持",
                scope="仅用于公开演示",
                confirmed_by="测试用户",
                user_confirmed=True,
            )
            text = path.read_text(encoding="utf-8")
            self.assertIn("这是人工确认的结论", text)
            self.assertIn("video_id=123", text)
            self.assertIn("（用户文字确认）", text)
            self.assertNotIn("不应被复制进正式笔记", text)
            curated = chunks.chunk_library(root)
            curated_rows = [row for row in curated if row["path"] == str(path)]
            self.assertTrue(curated_rows)
            self.assertTrue(all(row["source_layer"] == "curated" for row in curated_rows))
            self.assertTrue(all(row["review_status"] == "confirmed" for row in curated_rows))
            self.assertTrue(all(row["source_ids"] == ["123"] for row in curated_rows))
            self.assertNotIn("公开测试来源", "".join(row["text"] for row in curated_rows))
            self.assertNotIn("本地来源", "".join(row["text"] for row in curated_rows))
            event = json.loads((root / kn.EVENTS_NAME).read_text(encoding="utf-8"))
            self.assertEqual(event["source_ids"], ["123"])
            with self.assertRaises(FileExistsError):
                kn.promote(
                    root,
                    title="测试结论",
                    conclusion="不能覆盖",
                    source_ids=["123"],
                    rationale="测试",
                    scope="测试",
                    confirmed_by="测试用户",
                    user_confirmed=True,
                )

    def test_malformed_confirmed_note_does_not_embed_traceability_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "malformed.md"
            path.write_text(
                "---\n"
                "title: \"错误格式\"\n"
                "knowledge_status: \"confirmed\"\n"
                "source_ids: [\"123\"]\n"
                "---\n\n"
                "## 来源与可追溯性\n\n"
                "本地来源：C:\\\\secret\\\\source.md\n",
                encoding="utf-8",
            )
            self.assertEqual(chunks.chunk_file(path), [])


if __name__ == "__main__":
    unittest.main()
