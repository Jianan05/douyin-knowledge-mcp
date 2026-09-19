from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import impact_review


class ImpactReviewTests(unittest.TestCase):
    def _report(self, root: Path, *, cited: bool = False) -> Path:
        path = root / impact_review.REPORT_DIR / "report.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "title": "测试影响报告",
            "generated_at": "2026-09-19T16:00:00+12:00",
            "results": [{
                "source_id": "source-1", "source_title": "新来源",
                "source_path": str(root / "inbox" / "source.md"),
                "routing_status": "already_incorporated" if cited else "needs_relation_review",
                "candidates": [{
                    "score": 0.88, "note_title": "正式主题",
                    "note_path": str(root / "notes" / "topic.md"),
                    "material_preview": "新来源证据", "curated_preview": "原正式结论",
                    "material_chunk_id": "source-1#0", "curated_chunk_id": "topic#0",
                    "already_cited": cited,
                }],
            }],
        }, ensure_ascii=False), encoding="utf-8")
        return path

    def test_latest_report_skips_superseded_json(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            valid = self._report(root)
            old = valid.parent / "newer.json"
            old.write_text(json.dumps({"report_status": "superseded", "results": [{}]}), encoding="utf-8")
            self.assertEqual(impact_review.latest_report_path(root), valid)

    def test_decision_is_append_only_and_restored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report = self._report(root)
            _, _, items = impact_review.review_items(root, report)
            event = impact_review.save_decision(root, {
                "candidate_id": items[0]["candidate_id"],
                "relation": "refines", "action": "supplement",
                "note": "补充适用边界", "proposed_change": "增加一条限制条件。",
            }, report)
            self.assertEqual(event["relation"], "refines")
            restored = impact_review.review_items(root, report)[2][0]["decision"]
            self.assertEqual(restored["proposed_change"], "增加一条限制条件。")
            self.assertEqual(len((root / impact_review.EVENTS_NAME).read_text(encoding="utf-8").splitlines()), 1)

    def test_cited_candidate_is_locked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report = self._report(root, cited=True)
            item = impact_review.review_items(root, report)[2][0]
            self.assertTrue(item["decision"]["locked"])
            with self.assertRaisesRegex(ValueError, "已经被正式知识明确引用"):
                impact_review.save_decision(root, {
                    "candidate_id": item["candidate_id"],
                    "relation": "supports", "action": "no_change",
                }, report)

    def test_draft_requires_decision_and_never_edits_note(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report = self._report(root)
            note = root / "notes" / "topic.md"
            note.parent.mkdir()
            note.write_text("正式知识原文", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "还没有人工关系判断"):
                impact_review.render_draft(root, report)
            item = impact_review.review_items(root, report)[2][0]
            impact_review.save_decision(root, {
                "candidate_id": item["candidate_id"],
                "relation": "contradicts", "action": "supersede",
                "note": "原结论条件已变化", "proposed_change": "将旧结论标记作废并写明原因。",
            }, report)
            output, text = impact_review.render_draft(root, report)
            self.assertTrue(output.is_file())
            self.assertIn("讨论草案，不是正式知识", text)
            self.assertIn("（人工关系判断）", text)
            self.assertIn("（语义相似度路由，弱）", text)
            self.assertEqual(note.read_text(encoding="utf-8"), "正式知识原文")

    def test_page_exposes_decision_and_draft_controls(self):
        self.assertIn("/api/decision", impact_review.PAGE)
        self.assertIn("/api/draft", impact_review.PAGE)
        self.assertIn("不会直接修改正式知识", impact_review.PAGE)

    def test_http_api_lists_and_appends_decision(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report = self._report(root)
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                impact_review.make_handler(root, report),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib.request.urlopen(base + "/api/items") as response:
                    listed = json.loads(response.read().decode("utf-8"))
                candidate = listed["items"][0]
                body = json.dumps({
                    "candidate_id": candidate["candidate_id"],
                    "relation": "supports", "action": "source_only",
                    "note": "仅登记为佐证",
                }).encode("utf-8")
                request = urllib.request.Request(
                    base + "/api/decision", data=body,
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                with urllib.request.urlopen(request) as response:
                    saved = json.loads(response.read().decode("utf-8"))
                self.assertEqual(saved["relation"], "supports")
                self.assertTrue((root / impact_review.EVENTS_NAME).is_file())
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
