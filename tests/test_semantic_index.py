import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import semantic_index as si


class SemanticIndexTests(unittest.TestCase):
    def test_prepare_rows_excludes_delete_requests_and_hashes_text(self):
        source = [
            {
                "chunk_id": "1#0",
                "video_id": "1",
                "idx": 0,
                "text": "保留的素材",
                "review_status": "pending",
            },
            {
                "chunk_id": "2#0",
                "video_id": "2",
                "idx": 0,
                "text": "已经请求删除",
                "review_status": "delete_requested",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            rows, excluded = si.prepare_rows(Path(tmp), source)
        self.assertEqual([row["chunk_id"] for row in rows], ["1#0"])
        self.assertEqual(excluded["delete_requested"], 1)
        self.assertEqual(len(rows[0]["text_sha256"]), 64)
        self.assertTrue(rows[0]["quality_warning"])

    def test_fingerprint_changes_when_transcript_changes(self):
        base = {
            "chunk_id": "1#0",
            "review_status": "pending",
            "category": "",
            "path": "one.md",
        }
        first = [{**base, "text_sha256": "a"}]
        second = [{**base, "text_sha256": "b"}]
        self.assertNotEqual(si.fingerprint(first, "model"), si.fingerprint(second, "model"))

    def test_rank_rows_applies_status_filter(self):
        rows = [
            {"review_status": "pending", "category": "A"},
            {"review_status": "reference", "category": "A"},
            {"review_status": "reference", "category": "B"},
        ]
        vectors = np.asarray([[1.0, 0.0], [0.8, 0.2], [0.9, 0.1]], dtype=np.float32)
        ranked = si.rank_rows(
            rows,
            vectors,
            np.asarray([1.0, 0.0], dtype=np.float32),
            top_k=5,
            categories={"A"},
            review_states={"reference"},
        )
        self.assertEqual(len(ranked), 1)
        self.assertIs(ranked[0][1], rows[1])

    def test_rank_rows_keeps_material_and_curated_layers_separate(self):
        rows = [
            {"review_status": "reference", "category": "A", "source_layer": "material"},
            {"review_status": "confirmed", "category": "A", "source_layer": "curated"},
        ]
        vectors = np.asarray([[1.0, 0.0], [0.9, 0.1]], dtype=np.float32)
        ranked = si.rank_rows(
            rows,
            vectors,
            np.asarray([1.0, 0.0], dtype=np.float32),
            source_layers={"curated"},
        )
        self.assertEqual([row["source_layer"] for _, row in ranked], ["curated"])

    def test_index_status_detects_stale_source_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / si.INDEX_DIRNAME
            out.mkdir()
            stored_rows = [{
                "chunk_id": "1#0", "video_id": "1", "chunk_index": 0,
                "title": "one", "text": "old", "text_sha256": "old",
                "source": "", "path": "", "category": "", "tags": [],
                "review_status": "reference", "captured_at": "",
                "duration_seconds": 0, "quality_warning": False,
            }]
            model = "test-model"
            manifest = {
                "model": model,
                "chunk_count": 1,
                "video_count": 1,
                "source_fingerprint": si.fingerprint(stored_rows, model),
            }
            (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (out / "chunks.jsonl").write_text(json.dumps(stored_rows[0]) + "\n", encoding="utf-8")
            with (out / "vectors.npy").open("wb") as handle:
                np.save(handle, np.asarray([[1.0]], dtype=np.float32))

            changed = [{**stored_rows[0], "text": "new", "text_sha256": "new"}]
            with mock.patch.object(si, "prepare_rows", return_value=(changed, {})):
                status = si.index_status(root)

            self.assertEqual(status["freshness"], "stale")
            self.assertEqual(status["chunk_count_delta"], 0)

    def test_build_index_reuses_vectors_with_unchanged_text_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / si.INDEX_DIRNAME
            out.mkdir()
            old_rows = [
                {"chunk_id": "1#0", "video_id": "1", "text": "same", "text_sha256": "hash-a",
                 "review_status": "reference", "category": "", "path": "one.md"},
                {"chunk_id": "2#0", "video_id": "2", "text": "old", "text_sha256": "hash-b",
                 "review_status": "reference", "category": "", "path": "two.md"},
            ]
            model = "test-model"
            old_manifest = {
                "model": model,
                "chunk_count": 2,
                "video_count": 2,
                "source_fingerprint": si.fingerprint(old_rows, model),
            }
            (out / "manifest.json").write_text(json.dumps(old_manifest), encoding="utf-8")
            (out / "chunks.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in old_rows), encoding="utf-8"
            )
            with (out / "vectors.npy").open("wb") as handle:
                np.save(handle, np.asarray([[1.0, 0.0], [0.5, 0.5]], dtype=np.float32))
            new_rows = [
                old_rows[0],
                {**old_rows[1], "text": "new", "text_sha256": "hash-c"},
            ]
            with (
                mock.patch.object(si, "prepare_rows", return_value=(new_rows, {})),
                mock.patch.object(
                    si, "embed_texts", return_value=np.asarray([[0.0, 1.0]], dtype=np.float32)
                ) as embed,
            ):
                manifest = si.build_index(root, model_name=model)

            embed.assert_called_once_with(["new"], model_name=model, batch_size=16, device="")
            vectors = np.load(out / "vectors.npy", allow_pickle=False)
            np.testing.assert_array_equal(vectors, np.asarray([[1.0, 0.0], [0.0, 1.0]]))
            self.assertEqual(manifest["reused_vector_count"], 1)
            self.assertEqual(manifest["embedded_vector_count"], 1)


if __name__ == "__main__":
    unittest.main()
