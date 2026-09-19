"""Find curated notes that may be affected by selected material sources.

Semantic similarity is used only for candidate routing.  It cannot establish
whether a source supports, duplicates, refines or contradicts a conclusion;
that relation remains an explicit review decision.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import semantic_index
import review_book


DEFAULT_LIBRARY = Path.home() / "Desktop" / "DouyinNotes"
DEFAULT_THRESHOLD = 0.52


def pending_reviewed_source_ids(root: Path) -> list[str]:
    """Return queued, still-eligible sources that have not been scanned."""
    latest_queue: dict[str, tuple[int, dict]] = {}
    queue_path = root / review_book.IMPACT_QUEUE_NAME
    if queue_path.is_file():
        with queue_path.open(encoding="utf-8") as handle:
            for position, line in enumerate(handle):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                source_id = str(event.get("video_id") or "").strip()
                if source_id and event.get("event") in {"queued", "scan_completed"}:
                    latest_queue[source_id] = (position, event)

    states = review_book.load_states(root / review_book.STATE_NAME)
    eligible = review_book.IMPACT_REVIEW_STATES
    pending = [
        (position, source_id)
        for source_id, (position, event) in latest_queue.items()
        if event.get("event") == "queued"
        and str((states.get(source_id) or {}).get("review_status") or "") in eligible
    ]
    return [source_id for _, source_id in sorted(pending)]


def ensure_current_index(root: Path) -> dict:
    """Build a missing/stale index, reusing unchanged vectors."""
    try:
        status = semantic_index.index_status(root, check_freshness=True)
    except SystemExit:
        return semantic_index.build_index(root)
    if status.get("freshness") != "current":
        return semantic_index.build_index(root)
    return status


def record_completed_scan(root: Path, report: dict, report_path: Path) -> None:
    for source_id in report["source_ids"]:
        review_book.append_event(root / review_book.IMPACT_QUEUE_NAME, {
            "schema_version": 1,
            "event": "scan_completed",
            "video_id": source_id,
            "completed_at": report["generated_at"],
            "report_path": str(report_path),
            "index_fingerprint": report.get("index_fingerprint", ""),
        })


def _parse_time(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    for parser in (datetime.fromisoformat,):
        try:
            return parser(normalized)
        except ValueError:
            pass
    raise ValueError(f"无法解析时间：{value}")


def _row_time(row: dict) -> datetime | None:
    value = str(row.get("captured_at") or "").strip()
    if not value:
        return None
    try:
        return _parse_time(value)
    except ValueError:
        return None


def select_source_ids(
    rows: list[dict],
    *,
    source_ids: list[str] | None = None,
    since: str | None = None,
) -> list[str]:
    material_rows = [row for row in rows if row.get("source_layer", "material") == "material"]
    available = {str(row.get("video_id") or "") for row in material_rows}
    if source_ids:
        requested = list(dict.fromkeys(value.strip() for value in source_ids if value.strip()))
        missing = [source_id for source_id in requested if source_id not in available]
        if missing:
            raise ValueError("语义索引中没有素材：" + "、".join(missing))
        return requested
    if since:
        cutoff = _parse_time(since)
        selected: dict[str, datetime] = {}
        for row in material_rows:
            source_id = str(row.get("video_id") or "")
            captured = _row_time(row)
            if source_id and captured is not None:
                comparable_cutoff = cutoff
                if captured.tzinfo is None and cutoff.tzinfo is not None:
                    comparable_cutoff = cutoff.replace(tzinfo=None)
                elif captured.tzinfo is not None and cutoff.tzinfo is None:
                    captured = captured.replace(tzinfo=None)
                if captured >= comparable_cutoff:
                    selected[source_id] = min(captured, selected.get(source_id, captured))
        return [source_id for source_id, _ in sorted(selected.items(), key=lambda item: item[1])]
    raise ValueError("必须提供 --source-id 或 --since")


def score_impacts(
    rows: list[dict],
    vectors: np.ndarray,
    source_ids: list[str],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    top_k: int = 5,
) -> list[dict]:
    if vectors.ndim != 2 or vectors.shape[0] != len(rows):
        raise ValueError("索引行数与向量数量不一致")
    material_by_source: dict[str, list[int]] = defaultdict(list)
    curated_by_path: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        layer = row.get("source_layer", "material")
        if layer == "curated":
            curated_by_path[str(row.get("path") or row.get("title") or "")].append(index)
        elif str(row.get("video_id") or "") in source_ids:
            material_by_source[str(row.get("video_id"))].append(index)
    if not curated_by_path:
        raise ValueError("精选知识层还没有可比较的向量片段")

    results: list[dict] = []
    for source_id in source_ids:
        source_indices = material_by_source.get(source_id, [])
        if not source_indices:
            continue
        source_row = rows[source_indices[0]]
        candidates: list[dict] = []
        for note_path, curated_indices in curated_by_path.items():
            scores = np.asarray(
                vectors[np.asarray(source_indices)]
                @ vectors[np.asarray(curated_indices)].T,
                dtype=np.float32,
            )
            flat_index = int(np.argmax(scores))
            source_pos, curated_pos = np.unravel_index(flat_index, scores.shape)
            score = float(scores[source_pos, curated_pos])
            material_row = rows[source_indices[int(source_pos)]]
            curated_row = rows[curated_indices[int(curated_pos)]]
            cited_source_ids = {
                str(value) for value in (curated_row.get("source_ids") or [])
            }
            candidates.append({
                "score": score,
                "note_title": curated_row.get("title") or Path(note_path).stem,
                "note_path": note_path,
                "material_chunk_id": material_row.get("chunk_id"),
                "curated_chunk_id": curated_row.get("chunk_id"),
                "material_preview": str(material_row.get("text") or "")[:300],
                "curated_preview": str(curated_row.get("text") or "")[:300],
                "already_cited": source_id in cited_source_ids,
            })
        candidates.sort(key=lambda row: row["score"], reverse=True)
        kept = [candidate for candidate in candidates if candidate["score"] >= threshold][:max(1, top_k)]
        routing_status = "new_topic_candidate"
        if kept:
            cited = [candidate for candidate in kept if candidate.get("already_cited")]
            if cited:
                # An explicit source citation is stronger evidence than a
                # similarity guess.  Do not reopen an incorporated source
                # merely because it also resembles a broad adjacent note.
                kept = cited
                routing_status = "already_incorporated"
            else:
                routing_status = "needs_relation_review"
        results.append({
            "source_id": source_id,
            "source_title": source_row.get("title") or source_id,
            "source_path": source_row.get("path") or "",
            "review_status": source_row.get("review_status") or "pending",
            "routing_status": routing_status,
            "candidates": kept,
        })
    return results


def _safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value).strip(" .-")
    return value[:80] or "impact-scan"


def render_markdown(report: dict) -> str:
    lines = [
        f"# {report['title']}",
        "",
        f"> 生成时间：{report['generated_at']}",
        "> 状态：机器路由候选，尚未完成人工关系判断，不是正式知识。",
        "> 重要：语义相似度只能说明文本可能相关，不能证明重复、支持、补充或冲突。",
        "",
        "## 范围",
        "",
        f"- 输入来源：{len(report['results'])} 条",
        f"- 相似度阈值：{report['threshold']:.3f}",
        f"- 正式知识数量：{report['curated_note_count']} 条",
        "",
    ]
    for number, result in enumerate(report["results"], 1):
        lines.extend([
            f"## {number}. {result['source_title']}",
            "",
            f"- `source_id`：`{result['source_id']}`",
            f"- 审阅状态：`{result['review_status']}`",
            f"- 路由状态：`{result['routing_status']}`",
            f"- 本地来源：`{result['source_path']}`",
            "",
        ])
        if not result["candidates"]:
            lines.extend([
                "未达到阈值的正式知识候选；暂列为新主题候选，仍需人工判断是否只是检索漏召回。",
                "",
            ])
            continue
        for candidate_number, candidate in enumerate(result["candidates"], 1):
            if candidate.get("already_cited"):
                relation_line = "- 关系状态：`已由正式笔记 source_ids 明确引用`"
                action_line = "- 建议动作：`无需作为新增影响复核`"
            else:
                relation_line = "- 人工关系：`待填写：duplicate / supports / refines / contradicts / unrelated`"
                action_line = "- 建议动作：`待填写：不改 / 补充 / 作废替代 / 新建专题 / 仅登记来源`"
            lines.extend([
                f"### 候选 {candidate_number}：{candidate['note_title']}",
                "",
                f"- 相似度：`{candidate['score']:.4f}`",
                f"- 正式笔记：`{candidate['note_path']}`",
                f"- 命中片段：`{candidate['material_chunk_id']}` ↔ `{candidate['curated_chunk_id']}`",
                f"- 已被该正式知识引用：`{str(bool(candidate.get('already_cited'))).lower()}`",
                relation_line,
                action_line,
                "",
                f"> 新素材片段：{candidate['material_preview']}",
                "",
                f"> 正式知识片段：{candidate['curated_preview']}",
                "",
            ])
    lines.extend([
        "## 人工确认门",
        "",
        "本报告不会修改正式笔记。只有逐项核对原文、填写关系和建议动作，并由用户确认后，才能更新精选知识层。",
        "",
    ])
    return "\n".join(lines)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def build_report(
    root: Path,
    *,
    source_ids: list[str] | None,
    since: str | None,
    threshold: float,
    top_k: int,
    title: str,
) -> dict:
    status = semantic_index.index_status(root, check_freshness=True)
    if status.get("freshness") != "current":
        raise ValueError("语义索引已过期；先运行 semantic_index.py --build")
    manifest, rows, vectors = semantic_index.load_index(root)
    selected = select_source_ids(rows, source_ids=source_ids, since=since)
    if not selected:
        raise ValueError("指定范围没有找到素材")
    results = score_impacts(rows, vectors, selected, threshold=threshold, top_k=top_k)
    return {
        "schema_version": 1,
        "title": title,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "threshold": threshold,
        "top_k": top_k,
        "source_ids": selected,
        "index_fingerprint": manifest.get("source_fingerprint", ""),
        "curated_note_count": len({
            row.get("path") for row in rows if row.get("source_layer") == "curated"
        }),
        "meaning": "机器路由候选；相似度不代表知识关系",
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="查找新增素材可能影响的正式知识")
    parser.add_argument("--dir", type=Path, default=DEFAULT_LIBRARY)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--source-id", action="append", default=[])
    selection.add_argument("--since", help="选择该时间及之后采集的素材，如 2026-09-01")
    selection.add_argument(
        "--pending-reviewed",
        action="store_true",
        help="处理人工审阅后自动进入的待影响扫描队列",
    )
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--title", default="新增素材对正式知识的影响候选")
    parser.add_argument("--output", type=Path, help="Markdown 输出路径；默认写入 _讨论工作区/影响扫描")
    parser.add_argument("--json", action="store_true", help="同时把完整报告打印为 JSON")
    args = parser.parse_args()
    root = args.dir.expanduser().resolve()
    selected_source_ids = args.source_id or None
    if args.pending_reviewed:
        selected_source_ids = pending_reviewed_source_ids(root)
        if not selected_source_ids:
            print("没有待影响扫描的已审阅来源。")
            return 0
        ensure_current_index(root)
    report = build_report(
        root,
        source_ids=selected_source_ids,
        since=args.since,
        threshold=args.threshold,
        top_k=args.top_k,
        title=args.title,
    )
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    output = args.output or root / "_讨论工作区" / "影响扫描" / f"{timestamp}_{_safe_name(args.title)}.md"
    output = output.expanduser().resolve()
    _atomic_write(output, render_markdown(report))
    _atomic_write(output.with_suffix(".json"), json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if args.pending_reviewed:
        record_completed_scan(root, report, output)
    print(f"已生成影响候选：{output}")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
