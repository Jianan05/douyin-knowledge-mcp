from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import continuous_favorites as cf


class ContinuousProgressTests(unittest.TestCase):
    def test_reads_completed_cycle_without_transcript_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "progress.md"
            path.write_text(
                "\n".join(
                    [
                        "- 状态：**完成**",
                        "- 收藏清点：130 条",
                        "- 已完成：16 条",
                        "- 已跳过：114 条",
                        "- 失败：0 条",
                    ]
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                cf.read_progress(path),
                {
                    "status": "完成",
                    "total": 130,
                    "failed": 0,
                    "completed": 16,
                    "skipped": 114,
                },
            )

    def test_independent_log_has_timestamp_and_strips_terminal_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runner.log"
            cf.log("\x1b[31m[连续续跑] 测试消息\x1b[0m", path, echo=False)
            text = path.read_text(encoding="utf-8")
            self.assertRegex(text, r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}\]")
            self.assertIn("[连续续跑] 测试消息", text)
            self.assertNotIn("\x1b", text)


if __name__ == "__main__":
    unittest.main()
