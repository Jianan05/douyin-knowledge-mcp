from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import impact_scan
import review_book


class ImpactScanTests(unittest.TestCase):
    def test_pending_reviewed_queue_filters_scanned_and_ineligible_sources(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for source_id, status in (("new", "reference"), ("done", "deep_dive"), ("excluded", "exclude")):
                review_book.append_event(root / review_book.STATE_NAME, {
                    "video_id": source_id, "review_status": status,
                })
                review_book.append_event(root / review_book.IMPACT_QUEUE_NAME, {
                    "event": "queued", "video_id": source_id,
                })
            impact_scan.record_completed_scan(
                root,
                {
                    "source_ids": ["done"],
                    "generated_at": "2026-09-19T16:00:00+12:00",
                    "index_fingerprint": "fingerprint",
                },
                root / "report.md",
            )

            self.assertEqual(impact_scan.pending_reviewed_source_ids(root), ["new"])

            review_book.append_event(root / review_book.IMPACT_QUEUE_NAME, {
                "event": "queued", "video_id": "done",
            })
            self.assertEqual(
                impact_scan.pending_reviewed_source_ids(root),
                ["new", "done"],
            )

    def test_select_source_ids_validates_explicit_ids_and_since(self):
        rows = [
            {"video_id": "old", "source_layer": "material", "captured_at": "2026-09-01 10:00:00"},
            {"video_id": "new", "source_layer": "material", "captured_at": "2026-09-03 10:00:00"},
            {"video_id": "", "source_layer": "curated", "captured_at": "2026-09-04 10:00:00"},
        ]
        self.assertEqual(
            impact_scan.select_source_ids(rows, since="2026-09-02"),
            ["new"],
        )
        self.assertEqual(
            impact_scan.select_source_ids(rows, source_ids=["new", "new"]),
            ["new"],
        )
        with self.assertRaisesRegex(ValueError, "语义索引中没有素材"):
            impact_scan.select_source_ids(rows, source_ids=["missing"])

    def test_score_impacts_routes_but_does_not_infer_relation(self):
        rows = [
            {"chunk_id": "s1#0", "video_id": "s1", "title": "source", "path": "source.md",
             "review_status": "reference", "source_layer": "material", "text": "source text"},
            {"chunk_id": "note-a#0", "video_id": "", "title": "note a", "path": "a.md",
             "review_status": "confirmed", "source_layer": "curated", "text": "note a text",
             "source_ids": []},
            {"chunk_id": "note-b#0", "video_id": "", "title": "note b", "path": "b.md",
             "review_status": "confirmed", "source_layer": "curated", "text": "note b text"},
        ]
        vectors = np.asarray([
            [1.0, 0.0],
            [0.9, 0.1],
            [0.1, 0.9],
        ], dtype=np.float32)
        result = impact_scan.score_impacts(rows, vectors, ["s1"], threshold=0.5, top_k=2)[0]
        self.assertEqual(result["routing_status"], "needs_relation_review")
        self.assertEqual([row["note_title"] for row in result["candidates"]], ["note a"])
        self.assertNotIn("relation", result["candidates"][0])

    def test_already_cited_source_is_not_reported_as_new_impact(self):
        rows = [
            {"chunk_id": "s#0", "video_id": "s", "title": "source", "path": "source.md",
             "review_status": "reference", "source_layer": "material", "text": "source"},
            {"chunk_id": "n#0", "video_id": "", "title": "note", "path": "note.md",
             "review_status": "confirmed", "source_layer": "curated", "text": "note",
             "source_ids": ["s"]},
            {"chunk_id": "broad#0", "video_id": "", "title": "broad", "path": "broad.md",
             "review_status": "confirmed", "source_layer": "curated", "text": "broad",
             "source_ids": []},
        ]
        vectors = np.asarray([
            [1.0, 0.0],
            [1.0, 0.0],
            [0.9, 0.1],
        ], dtype=np.float32)
        result = impact_scan.score_impacts(rows, vectors, ["s"], threshold=0.5)[0]
        self.assertEqual(result["routing_status"], "already_incorporated")
        self.assertEqual([row["note_title"] for row in result["candidates"]], ["note"])
        self.assertTrue(result["candidates"][0]["already_cited"])

    def test_low_similarity_becomes_new_topic_candidate(self):
        rows = [
            {"chunk_id": "s#0", "video_id": "s", "title": "source", "path": "source.md",
             "source_layer": "material", "text": "source"},
            {"chunk_id": "n#0", "video_id": "", "title": "note", "path": "note.md",
             "source_layer": "curated", "text": "note"},
        ]
        vectors = np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
        result = impact_scan.score_impacts(rows, vectors, ["s"], threshold=0.5)[0]
        self.assertEqual(result["routing_status"], "new_topic_candidate")
        self.assertEqual(result["candidates"], [])

    def test_markdown_keeps_relation_as_manual_field(self):
        report = {
            "title": "test", "generated_at": "now", "threshold": 0.5,
            "curated_note_count": 1,
            "results": [{
                "source_id": "s", "source_title": "source", "source_path": "source.md",
                "review_status": "reference", "routing_status": "needs_relation_review",
                "candidates": [{
                    "score": 0.9, "note_title": "note", "note_path": "note.md",
                    "material_chunk_id": "s#0", "curated_chunk_id": "n#0",
                    "material_preview": "source", "curated_preview": "note",
                    "already_cited": False,
                }],
            }],
        }
        text = impact_scan.render_markdown(report)
        self.assertIn("不能证明重复、支持、补充或冲突", text)
        self.assertIn("待填写：duplicate / supports / refines / contradicts / unrelated", text)

    def test_markdown_does_not_reopen_an_explicitly_cited_source(self):
        report = {
            "title": "test", "generated_at": "now", "threshold": 0.5,
            "curated_note_count": 1,
            "results": [{
                "source_id": "s", "source_title": "source", "source_path": "source.md",
                "review_status": "reference", "routing_status": "already_incorporated",
                "candidates": [{
                    "score": 0.9, "note_title": "note", "note_path": "note.md",
                    "material_chunk_id": "s#0", "curated_chunk_id": "n#0",
                    "material_preview": "source", "curated_preview": "note",
                    "already_cited": True,
                }],
            }],
        }
        text = impact_scan.render_markdown(report)
        self.assertIn("已由正式笔记 source_ids 明确引用", text)
        self.assertIn("无需作为新增影响复核", text)
        self.assertNotIn("待填写：duplicate", text)


if __name__ == "__main__":
    unittest.main()
