from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import ingest
import server


class TimestampedTranscriptTests(unittest.TestCase):
    def test_native_segments_keep_time_raw_cleaned_and_asr_signals(self):
        prompt = server.build_initial_prompt(server.DEFAULT_TERMS)
        segments = [
            SimpleNamespace(
                start=1.25,
                end=4.5,
                text="这是有效内容。",
                avg_logprob=-0.21,
                no_speech_prob=0.03,
                compression_ratio=1.1,
                temperature=0.0,
            ),
            SimpleNamespace(
                start=4.5,
                end=7.0,
                text=prompt,
                avg_logprob=-0.8,
                no_speech_prob=0.4,
                compression_ratio=2.0,
                temperature=0.0,
            ),
            SimpleNamespace(
                start=7.0,
                end=8.0,
                text="谢谢观看",
                avg_logprob=-0.7,
                no_speech_prob=0.5,
                compression_ratio=1.0,
                temperature=0.0,
            ),
        ]
        info = SimpleNamespace(
            duration=8.0,
            language="zh",
            language_probability=0.98,
        )

        class FakeModel:
            def transcribe(self, _path, **_kwargs):
                return iter(segments), info

        model = FakeModel()
        progress = []
        with patch.object(server, "_load_model", return_value=model), patch.object(
            server, "_decoder", return_value=(model, {})
        ):
            result = server._transcribe_segments_sync(
                "unused.m4a",
                "base",
                on_segment=lambda end, total, text: progress.append((end, total, text)),
            )

        self.assertIsInstance(result, str)
        self.assertEqual(str(result), "这是有效内容。")
        self.assertEqual(len(result.segments), 3)
        self.assertEqual(result.segments[0]["start"], 1.25)
        self.assertEqual(result.segments[0]["end"], 4.5)
        self.assertEqual(result.segments[0]["raw_text"], "这是有效内容。")
        self.assertEqual(result.segments[0]["cleaned_text"], "这是有效内容。")
        self.assertEqual(result.segments[0]["avg_logprob"], -0.21)
        self.assertEqual(result.segments[1]["cleaned_text"], "")
        self.assertIn("prompt_echo", result.segments[1]["cleaning"])
        self.assertEqual(result.segments[2]["cleaned_text"], "")
        self.assertIn("hallucinated_boilerplate", result.segments[2]["cleaning"])
        self.assertEqual(result.asr["language"], "zh")
        self.assertTrue(result.asr["vad_filter"])
        self.assertEqual(progress[-1][0], 8.0)


class SourcePackageTests(unittest.TestCase):
    def test_package_is_atomic_machine_data_and_note_links_to_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lib = ingest.Library(root)
            transcript = server.TimestampedTranscript(
                "清洗后的正文",
                segments=[{
                    "segment_id": 0,
                    "start": 2.0,
                    "end": 5.0,
                    "raw_text": "原始正文",
                    "cleaned_text": "清洗后的正文",
                    "cleaning": [],
                    "avg_logprob": -0.2,
                    "no_speech_prob": 0.01,
                    "compression_ratio": 1.0,
                    "temperature": 0.0,
                }],
                asr={"model": "medium", "vad_filter": True},
            )
            meta = {
                "platform": "douyin",
                "video_id": "123456",
                "url": "https://www.douyin.com/video/123456",
                "title": "测试标题",
                "tags": ["测试"],
                "duration": 5.0,
                "chars": 7,
                "model": "medium",
                "device": "CPU",
            }

            path = lib.write_source_package(
                meta,
                transcript,
                screen_ocr="画面文字",
                screen_candidates=[("ProjectName", 3)],
            )

            self.assertEqual(path, root / "_source_packages" / "123456.json")
            self.assertFalse(path.with_suffix(".json.tmp").exists())
            package = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(package["schema_version"], 1)
            self.assertEqual(package["source"]["video_id"], "123456")
            self.assertEqual(package["segments"][0]["segment_id"], "123456#000000")
            self.assertEqual(package["segments"][0]["raw_text"], "原始正文")
            self.assertEqual(package["segments"][0]["cleaned_text"], "清洗后的正文")
            self.assertEqual(package["supplements"]["screen_ocr"], "画面文字")

            meta["source_package"] = path.relative_to(root).as_posix()
            note = lib.write_note(meta, str(transcript))
            note_text = note.read_text(encoding="utf-8")
            self.assertIn('source_package: "_source_packages/123456.json"', note_text)
            ingest.cmd_sync(lib)
            reloaded = ingest.Library(root)
            self.assertEqual(
                reloaded.known("123456")["source_package"],
                "_source_packages/123456.json",
            )

    def test_plain_legacy_text_does_not_claim_to_have_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = ingest.Library(Path(tmp))
            result = lib.write_source_package(
                {"video_id": "old", "platform": "douyin"},
                "旧纯文本",
            )
            self.assertIsNone(result)
            self.assertFalse((Path(tmp) / "_source_packages" / "old.json").exists())

    def test_video_ingest_writes_package_and_records_relative_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lib = ingest.Library(root)
            transcript = server.TimestampedTranscript(
                "这是一段足够长的测试转写文字，不会触发画面识别兜底。",
                segments=[{
                    "segment_id": 0,
                    "start": 0.0,
                    "end": 6.0,
                    "raw_text": "这是一段足够长的测试转写文字，不会触发画面识别兜底。",
                    "cleaned_text": "这是一段足够长的测试转写文字，不会触发画面识别兜底。",
                    "cleaning": [],
                    "avg_logprob": -0.1,
                    "no_speech_prob": 0.01,
                    "compression_ratio": 1.0,
                    "temperature": 0.0,
                }],
                asr={"model": "medium", "vad_filter": True},
            )

            async def fake_download(_url, out_dir, on_progress=None):
                return str(Path(out_dir) / "123.m4a"), "douyin"

            with patch.object(
                ingest.server, "_detect_platform", return_value="douyin"
            ), patch.object(
                ingest, "_resolve_video_id", return_value=(
                    "https://www.douyin.com/video/123", "123"
                )
            ), patch.object(
                ingest.server, "_download_transcription_media", side_effect=fake_download
            ), patch.object(
                ingest.server, "capture_meta", return_value={"title": "测试", "video_id": "123"}
            ), patch.object(
                ingest.server, "_transcribe_segments_sync", return_value=transcript
            ), patch.object(
                ingest, "_media_duration", return_value=6.0
            ), patch.object(
                ingest.server, "device_label", return_value="CPU"
            ):
                line = asyncio.run(
                    ingest.ingest_one(
                        "https://www.douyin.com/video/123",
                        lib,
                        "medium",
                        False,
                        no_screen=True,
                    )
                )

            self.assertTrue(line.startswith("[ok]"))
            package_path = root / "_source_packages" / "123.json"
            self.assertTrue(package_path.exists())
            self.assertEqual(
                lib.known("123")["source_package"], "_source_packages/123.json"
            )
            note_path = Path(lib.known("123")["path"])
            self.assertIn(
                'source_package: "_source_packages/123.json"',
                note_path.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
