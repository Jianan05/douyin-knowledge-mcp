from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import image_note
import public_media_demo
import server


class FakeTranscript(str):
    def __new__(cls):
        value = str.__new__(cls, "Example.")
        value.asr = {
            "model": "tiny",
            "language": "en",
            "duration": 0.88,
            "device": "CPU",
        }
        value.segments = [{
            "segment_id": 0,
            "start": 0.0,
            "end": 0.88,
            "raw_text": "Example.",
            "cleaned_text": "Example.",
            "cleaning": [],
        }]
        return value


class PublicMediaDemoTests(unittest.TestCase):
    def test_demo_runs_mixed_media_through_traceable_review_workflow(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "demo"
            with (
                patch.object(
                    server,
                    "_transcribe_segments_sync",
                    return_value=FakeTranscript(),
                ),
                patch.object(
                    image_note,
                    "ocr_bytes",
                    return_value=(
                        "PUBLIC DEMO\nEVIDENCE FIRST\nREVIEW BEFORE PROMOTION"
                    ),
                ),
            ):
                result = public_media_demo.create_demo(root, "tiny")

            manifest = json.loads(
                Path(result["manifest"]).read_text(encoding="utf-8")
            )
            self.assertFalse(manifest["private_data_used"])
            self.assertEqual(
                {row["kind"] for row in manifest["sources"]},
                {"audio", "image", "text"},
            )
            self.assertEqual(manifest["sources"][0]["license"], "CC0-1.0")
            self.assertTrue(Path(manifest["sources"][0]["source_package"]).is_file())
            note = Path(result["knowledge_note"]).read_text(encoding="utf-8")
            self.assertIn('knowledge_status: "confirmed"', note)
            for source_id in (
                public_media_demo.AUDIO_ID,
                public_media_demo.IMAGE_ID,
                public_media_demo.TEXT_ID,
            ):
                self.assertIn(source_id, note)

    def test_demo_refuses_nonempty_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "keep.txt").write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "拒绝覆盖"):
                public_media_demo.create_demo(root)


if __name__ == "__main__":
    unittest.main()
