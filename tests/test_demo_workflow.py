from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import demo_workflow


class DemoWorkflowTests(unittest.TestCase):
    def test_demo_creates_traceable_confirmed_note_without_private_data(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "demo"
            result = demo_workflow.create_demo(root)
            note = Path(result["knowledge_note"]).read_text(encoding="utf-8")
            self.assertIn('knowledge_status: "confirmed"', note)
            self.assertIn(demo_workflow.DEMO_ID, note)
            self.assertIn("public-demo-fixture", note)
            events = [
                json.loads(line)
                for line in Path(result["events"]).read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events[0]["event"], "knowledge_promoted")

    def test_demo_refuses_nonempty_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "keep.txt").write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "拒绝覆盖"):
                demo_workflow.create_demo(root)


if __name__ == "__main__":
    unittest.main()
