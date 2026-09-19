"""Read-only health report for a DouyinNotes library."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import review_book
import semantic_index


DEFAULT_LIBRARY = Path.home() / "Desktop" / "DouyinNotes"


def _latest_failures(path: Path) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    if not path.is_file():
        return latest
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            item_id = str(row.get("aweme_id") or row.get("video_id") or "").strip()
            if item_id:
                latest[item_id] = row
    return latest


def library_status(root: Path, check_semantic: bool = False) -> dict:
    root = root.expanduser().resolve()
    catalog = review_book.load_index(root / "index.jsonl")
    states = review_book.load_states(root / review_book.STATE_NAME)
    review_counts = Counter(
        str(row.get("review_status") or "pending") for row in states.values()
    )
    failures = _latest_failures(root / "_失败记录.jsonl")
    active_failures = sum(row.get("event") == "failed" for row in failures.values())
    notes_dir = root / "notes"
    source_packages = root / "_source_packages"
    assets = root / "assets"

    semantic: dict = {"available": False, "freshness": "missing"}
    manifest_path = root / semantic_index.INDEX_DIRNAME / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            semantic = {
                "available": True,
                "freshness": "not_checked",
                "built_at": manifest.get("built_at"),
                "chunk_count": int(manifest.get("chunk_count") or 0),
                "video_count": int(manifest.get("video_count") or 0),
                "model": manifest.get("model"),
            }
            if check_semantic:
                checked = semantic_index.index_status(root, check_freshness=True)
                semantic.update({
                    "freshness": checked.get("freshness"),
                    "current_chunk_count": checked.get("current_chunk_count"),
                    "chunk_count_delta": checked.get("chunk_count_delta"),
                })
        except (OSError, ValueError, json.JSONDecodeError):
            semantic = {"available": False, "freshness": "invalid_manifest"}

    total = len(catalog)
    reviewed = sum(count for state, count in review_counts.items() if state != "pending")
    return {
        "root": str(root),
        "indexed_items": total,
        "source_packages": len(list(source_packages.glob("*.json")))
        if source_packages.is_dir() else 0,
        "asset_files": sum(1 for path in assets.rglob("*") if path.is_file())
        if assets.exists() else 0,
        "reviewed_items": reviewed,
        "review_coverage_percent": round(reviewed * 100 / total, 1) if total else 0.0,
        "review_status_counts": dict(sorted(review_counts.items())),
        "curated_notes": sum(1 for path in notes_dir.rglob("*.md") if path.is_file())
        if notes_dir.is_dir() else 0,
        "knowledge_events": sum(
            1 for line in (root / "_knowledge_events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ) if (root / "_knowledge_events.jsonl").is_file() else 0,
        "active_failures": active_failures,
        "failure_items_seen": len(failures),
        "semantic_index": semantic,
    }


def render_status(status: dict) -> str:
    review = status["review_status_counts"]
    semantic = status["semantic_index"]
    review_text = "、".join(f"{name} {count}" for name, count in review.items()) or "无"
    semantic_text = "未建立"
    if semantic.get("available"):
        semantic_text = (
            f"{semantic.get('chunk_count', 0)} 片段 / {semantic.get('video_count', 0)} 作品；"
            f"新鲜度 {semantic.get('freshness')}"
        )
    return "\n".join([
        f"知识库：{status['root']}",
        f"入库作品：{status['indexed_items']}；来源包：{status['source_packages']}；视觉资产文件：{status['asset_files']}",
        f"人工审阅：{status['reviewed_items']}（{status['review_coverage_percent']}%）；{review_text}",
        f"精选知识：{status['curated_notes']} 条；确认事件：{status['knowledge_events']}",
        f"当前失败项：{status['active_failures']}（历史出现过 {status['failure_items_seen']} 项）",
        f"语义索引：{semantic_text}",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description="只读查看本地知识库健康状态")
    parser.add_argument("--dir", type=Path, default=DEFAULT_LIBRARY)
    parser.add_argument(
        "--check-semantic",
        action="store_true",
        help="扫描当前文本哈希并确认向量索引是否过期（不重建、不输出正文）",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    status = library_status(args.dir, check_semantic=args.check_semantic)
    print(json.dumps(status, ensure_ascii=False, indent=2) if args.json else render_status(status))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
