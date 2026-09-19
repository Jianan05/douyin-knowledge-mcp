from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chunks
from note_sections import clean_screenshot_ocr


class ScreenshotOcrCleaningTests(unittest.TestCase):
    def test_only_high_confidence_whole_ui_lines_are_removed(self):
        raw = """### 图 1
论文标题：《平台互动对择偶观的影响》
Viola
2026-08-30
粉丝 2.3万
+ 关注
问 AI
3470人赞同了该回答）
06人听过
研究显示，3470人赞同不等于结论可靠。
等我先问一下AI
“正文引语必须保留。”"""
        result = clean_screenshot_ocr(raw)

        self.assertTrue(result["applied"])
        self.assertEqual(
            [row["text"] for row in result["removed_lines"]],
            ["### 图 1", "粉丝 2.3万", "+ 关注", "问 AI", "3470人赞同了该回答）", "06人听过"],
        )
        for important in ("论文标题", "Viola", "2026-08-30", "研究显示", "等我先问一下AI", "正文引语"):
            self.assertIn(important, result["cleaned_text"])
        self.assertEqual([row["text"] for row in result["flagged_lines"]], [
            "研究显示，3470人赞同不等于结论可靠。", "等我先问一下AI",
        ])

    def test_plain_transcript_is_never_cleaned(self):
        raw = "请点赞关注。数据显示有3470人赞同这个观点。"
        result = clean_screenshot_ocr(raw)
        self.assertFalse(result["applied"])
        self.assertEqual(result["cleaned_text"], raw)
        self.assertEqual(result["removed_lines"], [])

    def test_ambiguous_plain_follow_line_is_preserved_and_flagged(self):
        result = clean_screenshot_ocr("### 图 1\n关注\n这是正文")
        self.assertIn("关注", result["cleaned_text"])
        self.assertEqual([row["text"] for row in result["flagged_lines"]], ["关注"])

    def test_chunks_use_cleaned_copy_without_changing_markdown(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sample.md"
            original = """---
video_id: "123"
title: "截图"
---
## 转写稿
### 图 1
粉丝 3万
123人赞同
这是需要进入知识库的正文，包含足够多的有效信息，不能因为截图界面噪声而被删除或覆盖。这里继续补足长度，确保能够生成一个检索块。
"""
            path.write_text(original, encoding="utf-8")
            rows = chunks.chunk_file(path)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertTrue(rows)
            self.assertNotIn("粉丝 3万", rows[0]["text"])
            self.assertNotIn("123人赞同", rows[0]["text"])
            self.assertIn("需要进入知识库的正文", rows[0]["text"])

    def test_technical_failure_placeholder_is_not_knowledge_content(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "failure.md"
            original = "## 转写稿\n\n（画面识别失败：RuntimeError）\n"
            path.write_text(original, encoding="utf-8")
            self.assertEqual(chunks.chunk_file(path), [])
            self.assertEqual(path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
