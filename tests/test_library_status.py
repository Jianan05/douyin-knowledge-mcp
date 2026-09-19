from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import library_status
import review_book


class LibraryStatusTests(unittest.TestCase):
    def test_status_uses_metadata_and_latest_failure_event(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rows = [
                {"video_id": "1", "title": "one"},
                {"video_id": "2", "title": "two"},
            ]
            (root / "index.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            review_book.append_event(root / review_book.STATE_NAME, {
                "video_id": "1", "review_status": "reference",
            })
            review_book.append_event(root / review_book.IMPACT_QUEUE_NAME, {
                "event": "queued", "video_id": "1",
            })
            (root / "_失败记录.jsonl").write_text(
                "\n".join([
                    json.dumps({"aweme_id": "1", "event": "failed"}),
                    json.dumps({"aweme_id": "1", "event": "resolved"}),
                    json.dumps({"aweme_id": "2", "event": "failed"}),
                ]) + "\n",
                encoding="utf-8",
            )
            notes = root / "notes"
            notes.mkdir()
            (notes / "confirmed.md").write_text("confirmed", encoding="utf-8")

            status = library_status.library_status(root)

            self.assertEqual(status["indexed_items"], 2)
            self.assertEqual(status["reviewed_items"], 1)
            self.assertEqual(status["review_coverage_percent"], 50.0)
            self.assertEqual(status["active_failures"], 1)
            self.assertEqual(status["failure_items_seen"], 2)
            self.assertEqual(status["curated_notes"], 1)
            self.assertEqual(status["pending_impact_scans"], 1)
            self.assertEqual(status["correction_candidates"], 0)
            self.assertEqual(status["active_corrections"], 0)
            self.assertEqual(status["semantic_index"]["freshness"], "missing")


if __name__ == "__main__":
    unittest.main()
