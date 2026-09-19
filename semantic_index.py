"""Build and query a local, traceable semantic index for DouyinNotes.

The index is a rebuildable derivative of source notes.  It deliberately keeps
raw material separate from curated knowledge: search results are evidence for
discussion, not conclusions that are automatically promoted to the knowledge
base.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterable

import numpy as np

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import chunks


DEFAULT_ROOT = Path.home() / "Desktop" / "DouyinNotes"
DEFAULT_MODEL = "BAAI/bge-m3"
INDEX_DIRNAME = "_semantic_index"
EXCLUDED_REVIEW_STATES = {"exclude", "delete_requested"}


def _json_line(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"


def _load_catalog(root: Path) -> dict[str, dict]:
    """Load non-secret source metadata from the library's public index."""
    catalog: dict[str, dict] = {}
    path = root / "index.jsonl"
    if not path.is_file():
        return catalog
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            video_id = str(row.get("video_id") or "")
            if video_id:
                catalog[video_id] = row
    return catalog


def prepare_rows(root: Path, source_rows: Iterable[dict] | None = None) -> tuple[list[dict], Counter]:
    """Create stable index rows and exclude material marked for removal."""
    catalog = _load_catalog(root)
    raw_rows = list(source_rows) if source_rows is not None else chunks.chunk_library(root)
    excluded: Counter = Counter()
    prepared: list[dict] = []
    for row in raw_rows:
        status = str(row.get("review_status") or "pending")
        if status in EXCLUDED_REVIEW_STATES:
            excluded[status] += 1
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            excluded["empty"] += 1
            continue
        video_id = str(row.get("video_id") or "")
        extra = catalog.get(video_id, {})
        item = {
            "chunk_id": str(row.get("chunk_id") or ""),
            "video_id": video_id,
            "chunk_index": int(row.get("idx") or 0),
            "title": str(row.get("title") or extra.get("title") or ""),
            "text": text,
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "source": str(row.get("source") or extra.get("url") or ""),
            "path": str(row.get("path") or extra.get("path") or ""),
            "category": str(row.get("category") or extra.get("category") or ""),
            "tags": list(row.get("tags") or extra.get("tags") or []),
            "review_status": status,
            "source_layer": str(row.get("source_layer") or "material"),
            "captured_at": str(extra.get("at") or ""),
            "duration_seconds": extra.get("duration"),
            "quality_warning": status in {"needs_correction", "pending"},
        }
        prepared.append(item)
    return prepared, excluded


def fingerprint(rows: Iterable[dict], model_name: str) -> str:
    digest = hashlib.sha256()
    digest.update(model_name.encode("utf-8"))
    digest.update(b"\0chunking-v1\0")
    for row in rows:
        stable = {
            "chunk_id": row["chunk_id"],
            "text_sha256": row["text_sha256"],
            "review_status": row["review_status"],
            "source_layer": row.get("source_layer") or "material",
            "category": row["category"],
            "path": row["path"],
        }
        digest.update(_json_line(stable).encode("utf-8"))
    return digest.hexdigest()


def embed_texts(
    texts: list[str], model_name: str = DEFAULT_MODEL, batch_size: int = 16, device: str = ""
) -> np.ndarray:
    import torch
    from sentence_transformers import SentenceTransformer

    selected_device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    model = SentenceTransformer(model_name, device=selected_device, local_files_only=True)
    vectors = model.encode(
        texts,
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=True,
    )
    return np.asarray(vectors, dtype=np.float32)


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_write_vectors(path: Path, vectors: np.ndarray) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as handle:
        np.save(handle, vectors, allow_pickle=False)
    os.replace(tmp, path)


def _incremental_vectors(
    rows: list[dict],
    *,
    model_name: str,
    batch_size: int,
    device: str,
    old_manifest: dict | None = None,
    old_rows: list[dict] | None = None,
    old_vectors: np.ndarray | None = None,
) -> tuple[np.ndarray, int, int]:
    """Reuse embeddings whose text hash and model are unchanged."""
    cache: dict[str, np.ndarray] = {}
    dimension = 0
    if (
        old_manifest
        and old_manifest.get("model") == model_name
        and old_rows is not None
        and old_vectors is not None
        and old_vectors.ndim == 2
        and old_vectors.shape[0] == len(old_rows)
    ):
        dimension = int(old_vectors.shape[1])
        for index, row in enumerate(old_rows):
            text_hash = str(row.get("text_sha256") or "")
            if text_hash and text_hash not in cache:
                cache[text_hash] = np.asarray(old_vectors[index], dtype=np.float32)

    reused_positions: dict[int, np.ndarray] = {}
    missing_positions: list[int] = []
    for index, row in enumerate(rows):
        cached = cache.get(str(row.get("text_sha256") or ""))
        if cached is None:
            missing_positions.append(index)
        else:
            reused_positions[index] = cached

    embedded = np.empty((0, dimension), dtype=np.float32)
    if missing_positions:
        embedded = embed_texts(
            [rows[index]["text"] for index in missing_positions],
            model_name=model_name,
            batch_size=batch_size,
            device=device,
        )
        if embedded.ndim != 2 or embedded.shape[0] != len(missing_positions):
            raise RuntimeError("向量数量与待计算片段数量不一致，拒绝写入索引。")
        if dimension and embedded.shape[1] != dimension:
            raise RuntimeError("新旧向量维度不一致，拒绝混合索引。请使用 --force 重建。")
        dimension = int(embedded.shape[1])
    if not dimension:
        raise RuntimeError("无法确定向量维度，拒绝写入索引。")

    vectors = np.empty((len(rows), dimension), dtype=np.float32)
    for position, vector in reused_positions.items():
        vectors[position] = vector
    for embedded_index, position in enumerate(missing_positions):
        vectors[position] = embedded[embedded_index]
    return vectors, len(reused_positions), len(missing_positions)


def build_index(
    root: Path,
    model_name: str = DEFAULT_MODEL,
    batch_size: int = 16,
    device: str = "",
    force: bool = False,
) -> dict:
    rows, excluded = prepare_rows(root)
    current_fingerprint = fingerprint(rows, model_name)
    out_dir = root / INDEX_DIRNAME
    manifest_path = out_dir / "manifest.json"
    rows_path = out_dir / "chunks.jsonl"
    vectors_path = out_dir / "vectors.npy"

    old_manifest: dict = {}
    old_rows: list[dict] | None = None
    old_vectors: np.ndarray | None = None

    if not force and manifest_path.is_file() and rows_path.is_file() and vectors_path.is_file():
        try:
            old_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            with rows_path.open(encoding="utf-8") as handle:
                old_rows = [json.loads(line) for line in handle if line.strip()]
            old_vectors = np.load(vectors_path, mmap_mode="r", allow_pickle=False)
        except (OSError, ValueError, json.JSONDecodeError):
            old_manifest = {}
            old_rows = None
            old_vectors = None
        if old_manifest.get("source_fingerprint") == current_fingerprint:
            print(f"索引未变化，复用 {old_manifest.get('chunk_count', 0)} 个片段：{out_dir}")
            return old_manifest

    if not rows:
        raise SystemExit("没有可索引的正文片段。")

    print(f"准备更新 {len(rows)} 个片段的索引（模型 {model_name}）", flush=True)
    vectors, reused_count, embedded_count = _incremental_vectors(
        rows,
        model_name=model_name,
        batch_size=batch_size,
        device=device,
        old_manifest=None if force else old_manifest,
        old_rows=None if force else old_rows,
        old_vectors=None if force else old_vectors,
    )
    print(f"向量复用 {reused_count}，新计算 {embedded_count}。", flush=True)
    # Windows does not allow atomic replacement while the old mmap is open.
    if isinstance(old_vectors, np.memmap):
        old_vectors._mmap.close()
        old_vectors = None

    out_dir.mkdir(parents=True, exist_ok=True)
    rows_text = "".join(_json_line(row) for row in rows)
    manifest = {
        "format_version": 1,
        "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "model": model_name,
        "dimension": int(vectors.shape[1]),
        "chunk_count": len(rows),
        "video_count": len({row["video_id"] for row in rows if row["video_id"]}),
        "source_fingerprint": current_fingerprint,
        "excluded_chunks": dict(excluded),
        "source_root": str(root.resolve()),
        "meaning": "检索素材索引；搜索结果不是已确认知识",
        "reused_vector_count": reused_count,
        "embedded_vector_count": embedded_count,
    }
    _atomic_write_text(rows_path, rows_text)
    _atomic_write_vectors(vectors_path, vectors)
    _atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(f"索引完成：{out_dir}", flush=True)
    return manifest


def load_index(root: Path) -> tuple[dict, list[dict], np.ndarray]:
    out_dir = root / INDEX_DIRNAME
    manifest_path = out_dir / "manifest.json"
    rows_path = out_dir / "chunks.jsonl"
    vectors_path = out_dir / "vectors.npy"
    if not (manifest_path.is_file() and rows_path.is_file() and vectors_path.is_file()):
        raise SystemExit("尚未建立素材向量索引，请先运行 --build。")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    with rows_path.open(encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    vectors = np.load(vectors_path, mmap_mode="r", allow_pickle=False)
    if vectors.ndim != 2 or vectors.shape[0] != len(rows):
        raise SystemExit("索引元数据与向量数量不一致，请用 --build --force 重建。")
    return manifest, rows, vectors


def index_status(root: Path, check_freshness: bool = True) -> dict:
    """Return manifest details plus whether source notes have changed.

    Loading an existing index alone cannot reveal that newly ingested notes are
    missing.  The freshness check reuses deterministic chunk preparation but
    never embeds text or changes the index.
    """
    manifest, rows, vectors = load_index(root)
    status = dict(manifest)
    status["stored_row_count"] = len(rows)
    status["stored_vector_count"] = int(vectors.shape[0])
    if not check_freshness:
        status["freshness"] = "not_checked"
        return status

    current_rows, excluded = prepare_rows(root)
    current_fingerprint = fingerprint(current_rows, str(manifest.get("model") or DEFAULT_MODEL))
    status.update({
        "freshness": "current"
        if current_fingerprint == manifest.get("source_fingerprint")
        else "stale",
        "current_chunk_count": len(current_rows),
        "current_video_count": len({row["video_id"] for row in current_rows if row["video_id"]}),
        "chunk_count_delta": len(current_rows) - int(manifest.get("chunk_count") or 0),
        "current_source_fingerprint": current_fingerprint,
        "current_excluded_chunks": dict(excluded),
    })
    return status


def rank_rows(
    rows: list[dict],
    vectors: np.ndarray,
    query_vector: np.ndarray,
    top_k: int = 12,
    categories: set[str] | None = None,
    review_states: set[str] | None = None,
    source_layers: set[str] | None = None,
) -> list[tuple[float, dict]]:
    allowed = []
    for idx, row in enumerate(rows):
        if categories and row.get("category") not in categories:
            continue
        if review_states and row.get("review_status") not in review_states:
            continue
        if source_layers and row.get("source_layer", "material") not in source_layers:
            continue
        allowed.append(idx)
    if not allowed:
        return []
    indices = np.asarray(allowed, dtype=np.int64)
    scores = np.asarray(vectors[indices] @ query_vector, dtype=np.float32)
    count = min(max(top_k, 1), len(indices))
    if count == len(indices):
        local = np.argsort(-scores)
    else:
        local = np.argpartition(-scores, count - 1)[:count]
        local = local[np.argsort(-scores[local])]
    return [(float(scores[pos]), rows[int(indices[pos])]) for pos in local]


def query_index(
    root: Path,
    query: str,
    top_k: int,
    categories: set[str] | None,
    review_states: set[str] | None,
    as_json: bool,
    device: str = "",
    source_layers: set[str] | None = None,
) -> list[tuple[float, dict]]:
    manifest, rows, vectors = load_index(root)
    query_vector = embed_texts([query], manifest["model"], batch_size=1, device=device)[0]
    ranked = rank_rows(
        rows, vectors, query_vector, top_k, categories, review_states, source_layers
    )
    if as_json:
        print(json.dumps([{"score": score, **row} for score, row in ranked], ensure_ascii=False, indent=2))
        return ranked
    print(f"# 检索：{query}\n")
    print("以下是素材证据候选，不是已确认知识。\n")
    for number, (score, row) in enumerate(ranked, 1):
        warning = " ⚠ 待校正/未审阅" if row.get("quality_warning") else ""
        print(f"## {number}. {row['title']}  ({score:.4f}){warning}")
        print(
            f"- 层级：{row.get('source_layer', 'material')}；"
            f"状态：{row['review_status']}；片段：{row['chunk_id']}"
        )
        print(f"- 来源：{row['source'] or row['path']}")
        print(f"\n{row['text']}\n")
    return ranked


def main() -> None:
    parser = argparse.ArgumentParser(description="构建和查询可追溯的抖音素材向量索引")
    parser.add_argument("--dir", type=Path, default=DEFAULT_ROOT, help="DouyinNotes 根目录")
    parser.add_argument("--build", action="store_true", help="建立或按内容变化重建索引")
    parser.add_argument("--force", action="store_true", help="强制重算全部向量")
    parser.add_argument("--query", help="检索一个具体问题")
    parser.add_argument("--top-k", type=int, default=12)
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--review-status", action="append", default=[])
    parser.add_argument(
        "--layer",
        action="append",
        choices=("material", "curated"),
        default=[],
        help="限定原始素材层或精选知识层；可重复指定",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--device", choices=("", "cuda", "cpu"), default="")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出检索结果")
    parser.add_argument("--status", action="store_true", help="显示已有索引状态并检查素材变化")
    parser.add_argument(
        "--no-freshness-check",
        action="store_true",
        help="只读取索引清单，不扫描当前素材",
    )
    args = parser.parse_args()
    root = args.dir.expanduser().resolve()

    if args.build:
        build_index(root, args.model, args.batch_size, args.device, args.force)
    if args.status:
        status = index_status(root, check_freshness=not args.no_freshness_check)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        if status.get("freshness") == "stale":
            print("[warn] 素材已变化，请运行 semantic_index.py --build 更新索引。", file=sys.stderr)
    if args.query:
        query_index(
            root,
            args.query,
            args.top_k,
            set(args.category) or None,
            set(args.review_status) or None,
            args.json,
            args.device,
            set(args.layer) or None,
        )
    if not (args.build or args.status or args.query):
        parser.print_help()


if __name__ == "__main__":
    main()
