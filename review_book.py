"""旧收藏小批量审阅状态本：独立记录人工决定，不改原始转录稿。"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))

import favorite_audit
from note_sections import transcript_section


DEFAULT_LIBRARY = Path.home() / "Desktop" / "DouyinNotes"
STATE_NAME = "_审阅状态.jsonl"
BOOK_NAME = "_旧收藏整理状态本.md"
TOMBSTONE_NAME = "_删除标记.jsonl"
REFERENCE_CLIP_JUDGMENT = "观点视频的嵌入素材音频漏转"
STATUSES = {
    "pending": "待审阅",
    "reference": "可参考",
    "deep_dive": "重点深挖",
    "needs_correction": "转录需修正",
    "exclude": "不纳入知识库",
    "delete_requested": "彻底删除（待二次确认）",
}
STATUS_ALIASES = {
    **{key: key for key in STATUSES},
    "待审阅": "pending",
    "可参考": "reference",
    "重点深挖": "deep_dive",
    "转录需修正": "needs_correction",
    "不纳入知识库": "exclude",
    "彻底删除": "delete_requested",
    "彻底删除（待二次确认）": "delete_requested",
}
_SPACE = re.compile(r"\s+")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _flat(value: object, limit: int = 280) -> str:
    return _SPACE.sub(" ", str(value or "")).strip()[:limit]


def _number(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def load_index(path: Path) -> list[dict]:
    """按 index 顺序读取每个 video_id 的最后一条记录。"""
    latest: dict[str, dict] = {}
    order: list[str] = []
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            video_id = str(row.get("video_id") or "").strip()
            if not video_id:
                continue
            if video_id not in latest:
                order.append(video_id)
            latest[video_id] = row
    return [latest[video_id] for video_id in order]


def load_states(path: Path) -> dict[str, dict]:
    """合并追加事件，返回每条作品的最新状态；原事件仍留在 JSONL 中。"""
    states: dict[str, dict] = {}
    if not path.is_file():
        return states
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            video_id = str(event.get("video_id") or "").strip()
            if not video_id:
                continue
            merged = dict(states.get(video_id) or {})
            merged.update(event)
            states[video_id] = merged
    return states


def append_event(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")


def load_tombstones(path: Path) -> set[str]:
    deleted: set[str] = set()
    if not path.is_file():
        return deleted
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("status") == "deleted" and row.get("video_id"):
                deleted.add(str(row["video_id"]))
    return deleted


def known_mapping_risk_ids(root: Path) -> set[str]:
    """保守排除已有强风险及旧式空标题记录；不据此删除或移动任何内容。"""
    risky: set[str] = set()
    for state_id, state in load_states(root / STATE_NAME).items():
        if state.get("mapping_status") or state.get("review_status") in {"exclude", "delete_requested"}:
            risky.add(state_id)
    quarantine = root / "_审计隔离"
    if quarantine.is_dir():
        for manifest in quarantine.glob("*/manifest.json"):
            try:
                rows = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for row in rows if isinstance(rows, list) else []:
                video_id = str(row.get("aweme_id") or row.get("video_id") or "")
                if video_id:
                    risky.add(video_id)
    for row in load_index(root / "index.jsonl"):
        if not str(row.get("title") or "").strip():
            risky.add(str(row.get("video_id") or ""))
    risky.discard("")
    return risky


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _rewrite_jsonl_without_id(path: Path, video_id: str) -> int:
    if not path.is_file():
        return 0
    kept: list[str] = []
    removed = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line.rstrip("\n"))
                continue
            row_id = str(row.get("video_id") or row.get("aweme_id") or "")
            if row_id == video_id:
                removed += 1
            else:
                kept.append(json.dumps(row, ensure_ascii=False, allow_nan=False))
    path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    return removed


def _scrub_markdown_id(path: Path, video_id: str) -> bool:
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    if video_id not in text:
        return False
    if path.parent.name == "视觉校准":
        blocks = re.split(r"(?=^## )", text, flags=re.MULTILINE)
        updated = "".join(block for block in blocks if video_id not in block)
    else:
        updated = "\n".join(
            line for line in text.splitlines() if video_id not in line
        ) + "\n"
    path.write_text(updated, encoding="utf-8")
    return True


def deletion_plan(root: Path, video_id: str) -> dict:
    """只列出可精确归属的删除目标；不靠模糊语义猜测。"""
    rows = {str(row.get("video_id") or ""): row for row in load_index(root / "index.jsonl")}
    state = load_states(root / STATE_NAME).get(video_id) or {}
    row = rows.get(video_id) or state
    paths: set[Path] = set()
    for value in (row.get("path"), row.get("source_package")):
        if not value:
            continue
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            path = root / path
        if _inside(root, path) and path.exists():
            paths.add(path.resolve())
    for candidate in (
        root / "_source_packages" / f"{video_id}.json",
        root / "assets" / video_id,
    ):
        if candidate.exists():
            paths.add(candidate.resolve())
    for directory in (root / "inbox", root / "notes", root / "_待分类", root / "_已替换", root / "_审计隔离"):
        if directory.is_dir():
            for candidate in directory.rglob(f"*{video_id}*"):
                if _inside(root, candidate):
                    paths.add(candidate.resolve())
    return {
        "video_id": video_id,
        "paths": sorted(paths, key=lambda path: len(path.parts), reverse=True),
        "jsonl": [
            root / "index.jsonl",
            root / "_失败记录.jsonl",
            root / "_待取消收藏.jsonl",
            root / "_内容类型修正.jsonl",
            root / STATE_NAME,
        ],
        "vector_caches": [PROJECT / "data" / "chunk_vectors.npz", PROJECT / "data" / "embeddings.npz"],
        "text_reports": [
            path for path in [
                root / "_失败记录.md",
                root / "话题目录.md",
                *((root / "视觉校准").glob("*.md") if (root / "视觉校准").is_dir() else []),
            ] if path.is_file()
        ],
        # 旧的人工话题稿没有来源 ID 映射，不能安全地判断哪段由该视频派生。
        "untraceable_aggregates": [
            path for path in (root / "话题笔记").glob("*.md")
        ] if (root / "话题笔记").is_dir() else [],
    }


def execute_delete(root: Path, video_id: str, confirmation: str) -> dict:
    """二次确认后删除精确归属内容，并留下不含原文的最小防回灌标记。"""
    state = load_states(root / STATE_NAME).get(video_id) or {}
    if state.get("review_status") != "delete_requested":
        raise ValueError("必须先把该作品标为“彻底删除（待二次确认）”")
    if confirmation != video_id:
        raise ValueError("二次确认失败：--confirm-video-id 必须与作品 ID 完全一致")
    if video_id in load_tombstones(root / TOMBSTONE_NAME):
        raise ValueError("该作品已经彻底删除并留有防回灌标记")

    plan = deletion_plan(root, video_id)
    removed_paths: list[str] = []
    for path in plan["paths"]:
        if not _inside(root, path) or not path.exists():
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        removed_paths.append(str(path))
    removed_rows = 0
    for path in plan["jsonl"]:
        removed_rows += _rewrite_jsonl_without_id(path, video_id)
    invalidated_vectors = []
    for path in plan["vector_caches"]:
        if path.is_file():
            path.unlink()
            invalidated_vectors.append(str(path))
    scrubbed_reports = [
        str(path) for path in plan["text_reports"]
        if _scrub_markdown_id(path, video_id)
    ]
    append_event(root / TOMBSTONE_NAME, {
        "schema_version": 1,
        "video_id": video_id,
        "status": "deleted",
        "deleted_at": _now(),
    })
    render_book(root)
    return {
        "removed_paths": removed_paths,
        "removed_rows": removed_rows,
        "invalidated_vectors": invalidated_vectors,
        "scrubbed_reports": scrubbed_reports,
        "untraceable_aggregate_count": len(plan["untraceable_aggregates"]),
    }


def set_quality_note(
    root: Path,
    video_id: str,
    note: str,
    source: str = "仅检查现有文字，未对照原片（推断，未证实）",
) -> None:
    states = load_states(root / STATE_NAME)
    if video_id not in states:
        raise ValueError(f"状态本中没有作品 {video_id}；先运行 prepare")
    append_event(root / STATE_NAME, {
        "schema_version": 1,
        "event": "quality_sampled",
        "video_id": video_id,
        "quality_review_note": _flat(note, 2000),
        "quality_review_at": _now(),
        "quality_review_source": _flat(source, 500),
    })
    render_book(root)


def confirm_mapping_mismatch(root: Path, video_id: str, note: str) -> None:
    """保留原稿但停止下游使用，记录作品 ID 与稿件内容错配的强证据。"""
    states = load_states(root / STATE_NAME)
    state = states.get(video_id)
    if not state:
        raise ValueError(f"状态本中没有作品 {video_id}；先运行 prepare")
    quality = dict(state.get("quality") or {})
    flags = list(quality.get("flags") or [])
    warning = "已确认作品—源稿映射错配；原稿保留，禁止进入知识库，待单条重新获取"
    if warning not in flags:
        flags.append(warning)
    quality["flags"] = flags
    quality["verification"] = "映射错配已确认，等待按目标 aweme_id 单条修复"
    append_event(root / STATE_NAME, {
        "schema_version": 1,
        "event": "mapping_mismatch_confirmed",
        "video_id": video_id,
        "review_status": "exclude",
        "human_note": _flat(note, 2000),
        "quality": quality,
        "mapping_status": "confirmed_mismatch",
        "mapping_source": "用户逐条打开原片核对 + 历史捕获代码逐行核对（强）",
        "mapping_checked_at": _now(),
    })
    render_book(root)


def transcript_body(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return transcript_section(text) or ""


def refresh_batch_quality(root: Path, limit: int = 15) -> dict:
    """重新计算首批预览；只追加修正事件，不改原始 Markdown。"""
    states = load_states(root / STATE_NAME)
    rows = sorted(
        states.values(),
        key=lambda row: (str(row.get("prepared_at") or ""), int(row.get("batch_order") or 0)),
    )[:limit]
    index = {str(row.get("video_id") or ""): row for row in load_index(root / "index.jsonl")}
    changed: list[str] = []
    related_notes_removed: list[str] = []
    preview_caps_removed: list[str] = []
    for state in rows:
        video_id = str(state.get("video_id") or "")
        source = dict(index.get(video_id) or state)
        source.setdefault("path", state.get("path"))
        quality = quality_snapshot(root, source)
        old_quality = state.get("quality") or {}
        if quality.get("preview") == old_quality.get("preview"):
            continue
        note_path = Path(str(source.get("path") or ""))
        raw_text = note_path.read_text(encoding="utf-8") if note_path.is_file() else ""
        issues: list[str] = []
        if re.search(r"^##\s+相关笔记\s*$", raw_text, flags=re.MULTILINE):
            related_notes_removed.append(video_id)
            issues.append("元数据/关联笔记混入转录")
        if len(str(old_quality.get("preview") or "")) < len(str(quality.get("preview") or "")):
            preview_caps_removed.append(video_id)
            issues.append("审阅预览被 280 字上限截短")
        append_event(root / STATE_NAME, {
            "schema_version": 1,
            "event": "preview_sanitized",
            "video_id": video_id,
            "quality": quality,
            "pipeline_issues": issues,
            "pipeline_issue": "；".join(issues),
            "pipeline_issue_resolution": "转写稿只提取到下一个 Markdown 二级标题之前，并为审阅页提供完整区块；原文件未修改",
            "pipeline_issue_source": "首批 Markdown 逐文件标题边界扫描（强）",
            "quality_refreshed_at": _now(),
        })
        changed.append(video_id)
    render_book(root)
    return {
        "changed": changed,
        "related_notes_removed": related_notes_removed,
        "preview_caps_removed": preview_caps_removed,
    }


def canonical_url(row: dict) -> str:
    video_id = str(row.get("video_id") or "").strip()
    old_url = str(row.get("url") or "")
    route = "note" if "/note/" in old_url or "图文" in str(row.get("platform") or "") else "video"
    return f"https://www.douyin.com/{route}/{video_id}" if video_id else old_url


def quality_snapshot(root: Path, row: dict) -> dict:
    path = Path(str(row.get("path") or ""))
    body = transcript_body(path) if path.is_file() else ""
    from note_sections import clean_screenshot_ocr, is_technical_failure_placeholder
    cleaning = clean_screenshot_ocr(body)
    technical_failure = is_technical_failure_placeholder(cleaning["cleaned_text"])
    cleaned_body = "" if technical_failure else cleaning["cleaned_text"]
    chars = int(_number(row.get("chars")))
    duration = _number(row.get("duration"))
    flags: list[str] = []
    if not path.is_file():
        flags.append("索引对应的 Markdown 不存在")
    if not body:
        flags.append("转写稿为空或缺少转写稿章节")
    if technical_failure:
        flags.append("原始稿只有画面 OCR 失败占位符，不能当作作品正文")
    if body and len(_flat(body, 1000)) < 20:
        flags.append("转写内容过短")
    if duration >= 20 and chars / max(duration, 1) < 0.35:
        flags.append("单位时长文字偏少，可能需要结合画面或复核漏转")
    if path.is_file() and favorite_audit.has_prompt_echo(path):
        flags.append("检测到明显提示词回声")

    package_value = str(row.get("source_package") or "").strip()
    package_path = root / package_value if package_value else None
    has_timestamps = bool(package_path and package_path.is_file())
    if technical_failure:
        verification = "无可用作品正文；原始稿仅含画面 OCR 技术失败占位符"
    elif flags:
        verification = "自动检查发现风险，需看原片"
    elif has_timestamps:
        verification = "已有时间戳来源包，尚未经人工对照原片"
    else:
        verification = "旧式纯文本稿，无时间戳，尚未经人工对照原片"
    return {
        "verification": verification,
        "flags": flags,
        # 审阅页用于逐条核对，不能用通用摘要的 280 字上限冒充完整转写。
        # 仍只取“转写稿”区块，不包含后续关联笔记等附加区块。
        "preview": cleaned_body[:50_000].strip(),
        "original_preview": body[:50_000].strip(),
        "preview_truncated": len(cleaned_body) > 50_000,
        "original_preview_truncated": len(body) > 50_000,
        "ocr_cleaning": {
            "applied": cleaning["applied"],
            "removed_lines": cleaning["removed_lines"],
            "flagged_lines": cleaning["flagged_lines"],
        },
        "content_availability": "technical_failure_placeholder" if technical_failure else "usable",
        "has_timestamp_package": has_timestamps,
    }


def prepare_batch(
    root: Path,
    limit: int = 15,
    randomize: bool = False,
    batch_id: str | None = None,
    start_order: int = 1,
    video_ids: list[str] | None = None,
) -> list[str]:
    state_path = root / STATE_NAME
    states = load_states(state_path)
    tombstones = load_tombstones(root / TOMBSTONE_NAME)
    risky = known_mapping_risk_ids(root)
    indexed_rows = load_index(root / "index.jsonl")
    candidates: list[dict] = []
    selection_method = "system_random" if randomize else "index_order"
    if video_ids is not None:
        requested = list(dict.fromkeys(str(value).strip() for value in video_ids if str(value).strip()))
        catalog = {str(row.get("video_id") or ""): row for row in indexed_rows}
        missing = [video_id for video_id in requested if video_id not in catalog]
        if missing:
            raise ValueError("索引中没有作品：" + "、".join(missing))
        unavailable = [
            video_id for video_id in requested
            if video_id in states or video_id in tombstones or video_id in risky
        ]
        if unavailable:
            raise ValueError("作品已在状态本、删除标记或风险清单中：" + "、".join(unavailable))
        candidates = [catalog[video_id] for video_id in requested]
        selection_method = "explicit_ids"
        limit = len(candidates)
    else:
        for row in indexed_rows:
            video_id = str(row.get("video_id") or "")
            if video_id in states or video_id in tombstones or video_id in risky:
                continue
            note_path = Path(str(row.get("path") or ""))
            if note_path.is_file():
                candidates.append(row)
    if randomize and video_ids is None:
        random.SystemRandom().shuffle(candidates)
    batch_id = batch_id or datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    chosen: list[str] = []
    for row in candidates:
        video_id = str(row.get("video_id") or "")
        note_path = Path(str(row.get("path") or ""))
        quality = quality_snapshot(root, row)
        if not quality.get("preview"):
            append_event(state_path, {
                "schema_version": 1,
                "event": "candidate_rejected",
                "video_id": video_id,
                "review_status": "exclude",
                "review_batch_id": batch_id,
                "selection_method": selection_method,
                "title": _flat(row.get("title"), 500),
                "url": canonical_url(row),
                "path": str(note_path),
                "quality": quality,
                "candidate_rejection": "现有 Markdown 没有可识别的转写稿正文，不能进入可审批次",
                "candidate_rejection_source": "Markdown 标题边界扫描（强）",
                "prepared_at": _now(),
            })
            continue
        append_event(state_path, {
            "schema_version": 1,
            "event": "prepared",
            "video_id": video_id,
            "review_status": "pending",
            "human_note": "",
            "prepared_at": _now(),
            "review_batch_id": batch_id,
            "selection_method": selection_method,
            "batch_order": start_order + len(chosen),
            "title": _flat(row.get("title"), 500),
            "url": canonical_url(row),
            "tags": row.get("tags") or [],
            "duration": _number(row.get("duration")),
            "chars": int(_number(row.get("chars"))),
            "path": str(note_path),
            "source_package": str(row.get("source_package") or ""),
            "quality": quality,
        })
        chosen.append(video_id)
        if len(chosen) >= max(1, limit):
            break
    render_book(root)
    return chosen


def set_status(root: Path, video_id: str, status: str, note: str = "") -> None:
    normalized = STATUS_ALIASES.get(status)
    if not normalized:
        raise ValueError("状态必须是：" + " / ".join(STATUSES.values()))
    states = load_states(root / STATE_NAME)
    if video_id not in states:
        raise ValueError(f"状态本中没有作品 {video_id}；先运行 prepare")
    append_event(root / STATE_NAME, {
        "schema_version": 1,
        "event": "reviewed",
        "video_id": video_id,
        "review_status": normalized,
        "human_note": _flat(note, 2000),
        "reviewed_at": _now(),
        "review_source": "用户口头或文字确认",
    })
    render_book(root)


def set_annotations(
    root: Path,
    video_id: str,
    *,
    video_types: list[str] | None = None,
    human_note: str | None = None,
    append_judgment: str | None = None,
    allowed_types: set[str] | None = None,
    allowed_judgments: set[str] | None = None,
) -> dict:
    """追加保存人工类型/备注，不携带也不覆盖审阅状态。"""
    states = load_states(root / STATE_NAME)
    if video_id not in states:
        raise ValueError(f"状态本中没有作品 {video_id}；先运行 prepare")
    event: dict = {
        "schema_version": 1,
        "event": "annotations_updated",
        "video_id": video_id,
        "annotations_updated_at": _now(),
        "annotation_source": "本地审阅页",
    }
    changed = False
    if video_types is not None:
        if not isinstance(video_types, list) or any(not isinstance(value, str) for value in video_types):
            raise ValueError("视频类型必须是字符串列表")
        normalized = list(dict.fromkeys(value.strip() for value in video_types if value.strip()))
        if allowed_types is not None and any(value not in allowed_types for value in normalized):
            raise ValueError("包含不支持的视频类型")
        event["video_types"] = normalized
        changed = True
    if human_note is not None:
        event["human_note"] = _flat(human_note, 2000)
        changed = True
    if append_judgment is not None:
        judgment = append_judgment.strip()
        if not judgment or (allowed_judgments is not None and judgment not in allowed_judgments):
            raise ValueError("不支持的常用判断")
        existing = str(event.get("human_note", states[video_id].get("human_note") or "")).strip()
        if judgment not in existing:
            event["human_note"] = _flat(f"{existing}；{judgment}" if existing else judgment, 2000)
        if judgment == REFERENCE_CLIP_JUDGMENT:
            event["reference_clip_segment"] = {
                "status": "awaiting_media_review",
                "time_range": None,
                "audio": "用户确认引用片段有语音",
                "visual": "尚未抽取关键帧",
                "transcript_candidate": None,
                "comment_segment": "现有主转录暂按作者评论段保留",
                "source": "人工审阅标记；未取得媒体前不生成内容",
            }
        changed = True
    if not changed:
        raise ValueError("没有可保存的类型或备注")
    append_event(root / STATE_NAME, event)
    render_book(root)
    return load_states(root / STATE_NAME)[video_id]


def render_book(root: Path) -> Path:
    states = load_states(root / STATE_NAME)
    tombstones = load_tombstones(root / TOMBSTONE_NAME)
    rows = sorted(
        states.values(),
        key=lambda row: (str(row.get("prepared_at") or ""), int(row.get("batch_order") or 0)),
    )
    counts = {key: 0 for key in STATUSES}
    for row in rows:
        status = str(row.get("review_status") or "pending")
        counts[status if status in counts else "pending"] += 1

    lines = [
        "# 旧收藏整理状态本",
        "",
        "> 本文件由 `review_book.py` 从追加式状态日志生成。原始转录、来源包、画面资产和抖音收藏均不会因审阅决定而删除。",
        "",
        "## 状态概览",
        "",
        *[f"- {label}：{counts[key]} 条" for key, label in STATUSES.items()],
        f"- 已彻底删除（仅留防回灌 ID）：{len(tombstones)} 条",
        "",
        "## 记录方式",
        "",
        "查看原片后，可让 Codex 记录，也可运行：",
        "",
        "```powershell",
        '.\\runtime\\python\\python.exe review_book.py set <video_id> 可参考 --note "值得保留的原因"',
        "```",
        "",
        "“不纳入知识库”只会让下游分块/RAG 跳过该作品，不会删除任何原始数据，也不会取消抖音收藏。",
        "“彻底删除”只是第一重申请；随后必须再次输入完全一致的视频 ID 才会执行。",
        "",
        "## 待审条目",
        "",
    ]
    for index, row in enumerate(rows, 1):
        status = str(row.get("review_status") or "pending")
        quality = row.get("quality") or {}
        flags = quality.get("flags") or []
        tags = "、".join(str(tag) for tag in (row.get("tags") or [])) or "无"
        note = str(row.get("human_note") or "").strip() or "（尚未记录）"
        video_types = "、".join(str(tag) for tag in (row.get("video_types") or [])) or "（尚未选择）"
        reference_clip = row.get("reference_clip_segment") or {}
        quality_note = str(row.get("quality_review_note") or "").strip() or "（尚未抽样检查）"
        preview = str(quality.get("preview") or "").strip() or "（无可用转写预览）"
        lines.extend([
            f"### {index:02d}. {row.get('title') or row.get('video_id')}",
            "",
            f"- 作品 ID：`{row.get('video_id')}`",
            f"- 原视频：[在抖音打开]({row.get('url')})",
            f"- 当前决定：**{STATUSES.get(status, status)}**",
            f"- 自动标签：{tags}",
            f"- 人工视频类型：{video_types}",
            f"- 引用素材段：{reference_clip.get('status') or '（未标记）'}",
            f"- 时长/字数：{_number(row.get('duration')):.1f} 秒 / {int(_number(row.get('chars')))} 字",
            f"- 转录核验：{quality.get('verification') or '尚未核验'}",
            f"- 自动风险：{'；'.join(str(flag) for flag in flags) if flags else '未发现明显静态风险'}",
            f"- 文字抽查：{quality_note}",
            f"- 抽查依据：{row.get('quality_review_source') or '尚无'}",
            f"- 人工备注：{note}",
            "",
            "转写预览：",
            "",
            f"> {preview}",
            "",
        ])
    path = root / BOOK_NAME
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="生成并维护旧收藏小批量审阅状态本")
    parser.add_argument("--dir", type=Path, default=DEFAULT_LIBRARY)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="抽取下一批尚未进入状态本的旧收藏")
    prepare.add_argument("--limit", type=int, default=15)
    prepare.add_argument("--random", action="store_true", help="使用系统随机源抽取，不按主题或索引优先")
    prepare_ids_parser = sub.add_parser("prepare-ids", help="按明确作品 ID 准备审阅，不依赖索引顺序")
    prepare_ids_parser.add_argument("video_ids", nargs="+")
    set_parser = sub.add_parser("set", help="记录一条人工审阅决定")
    set_parser.add_argument("video_id")
    set_parser.add_argument("status")
    set_parser.add_argument("--note", default="")
    flag_parser = sub.add_parser("flag", help="记录仅基于文字预览的疑似转录问题")
    flag_parser.add_argument("video_id")
    flag_parser.add_argument("--note", required=True)
    mismatch_parser = sub.add_parser("mapping-mismatch", help="确认作品与源稿错配并停止下游使用")
    mismatch_parser.add_argument("video_id")
    mismatch_parser.add_argument("--note", required=True)
    delete_parser = sub.add_parser("delete", help="执行已申请的彻底删除（二次确认）")
    delete_parser.add_argument("video_id")
    delete_parser.add_argument("--confirm-video-id", required=True)
    sub.add_parser("render", help="从追加状态日志重新生成 Markdown 状态本")
    sub.add_parser("refresh", help="重新计算首批预览并剥离转写稿后的附加区块")
    args = parser.parse_args()
    root = args.dir.expanduser().resolve()
    if args.command == "prepare":
        chosen = prepare_batch(root, args.limit, randomize=args.random)
        print(f"已准备 {len(chosen)} 条：{root / BOOK_NAME}")
    elif args.command == "prepare-ids":
        chosen = prepare_batch(root, video_ids=args.video_ids)
        print(f"已按指定 ID 准备 {len(chosen)} 条：{root / BOOK_NAME}")
    elif args.command == "set":
        set_status(root, args.video_id, args.status, args.note)
        print(f"已记录 {args.video_id}：{STATUSES[STATUS_ALIASES[args.status]]}")
    elif args.command == "flag":
        set_quality_note(root, args.video_id, args.note)
        print(f"已记录文字抽查：{args.video_id}")
    elif args.command == "mapping-mismatch":
        confirm_mapping_mismatch(root, args.video_id, args.note)
        print(f"已隔离映射错配：{args.video_id}（原稿未移动或删除）")
    elif args.command == "delete":
        result = execute_delete(root, args.video_id, args.confirm_video_id)
        print(
            f"已彻底删除 {args.video_id}：文件 {len(result['removed_paths'])}，"
            f"记录 {result['removed_rows']}，向量缓存 {len(result['invalidated_vectors'])}。"
        )
        if result["untraceable_aggregate_count"]:
            print(
                f"[warn] 现有 {result['untraceable_aggregate_count']} 份旧话题综合稿没有来源 ID 映射，"
                "无法可靠判断是否含该作品的改写内容，需人工复核。"
            )
    elif args.command == "refresh":
        result = refresh_batch_quality(root)
        print(
            f"已刷新首批预览：共修正 {len(result['changed'])} 条；"
            f"其中剥离关联笔记 {len(result['related_notes_removed'])} 条，"
            f"解除旧 280 字显示上限 {len(result['preview_caps_removed'])} 条；原始 Markdown 未修改。"
        )
    else:
        print(f"已重新生成：{render_book(root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
