from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_topic_packet


class TopicPacketTests(unittest.TestCase):
    def test_extract_transcript_stops_at_any_next_level_two_heading(self):
        text = "# 标题\n\n## 转写稿\n\n保留正文\n\n## 任意后续章节\n\n不能混入"
        self.assertEqual(build_topic_packet.extract_transcript(text), "保留正文")

    def test_extract_transcript_reports_missing_section(self):
        self.assertEqual(
            build_topic_packet.extract_transcript("# 没有转写稿"),
            "（未找到转写稿段落）",
        )


if __name__ == "__main__":
    unittest.main()
