"""Promote user-confirmed conclusions into the curated knowledge layer.

This module intentionally does not summarize transcripts.  It records a
conclusion supplied by the user and binds it to reviewed source IDs, keeping
extraction, retrieval, discussion and confirmed knowledge as separate layers.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import review_book


DEFAULT_LIBRARY = Path.home() / "Desktop" / "DouyinNotes"
EVENTS_NAME = "_knowledge_events.jsonl"
ALLOWED_SOURCE_STATES = {"reference", "deep_dive"}
_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _catalog(root: Path) -> dict[str, dict]:
    return {
        str(row.get("video_id")): row
        for row in review_book.load_index(root / "index.jsonl")
        if row.get("video_id")
    }


def _filename(title: str) -> str:
    name = _UNSAFE_FILENAME.sub("-", title).strip(" .-")
    name = re.sub(r"\s+", " ", name)
    if not name:
        raise ValueError("标题不能只包含文件名非法字符")
    return name[:100] + ".md"


def _json_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def promote(
    root: Path,
    *,
    title: str,
    conclusion: str,
    source_ids: list[str],
    rationale: str,
    scope: str,
    confirmed_by: str,
    user_confirmed: bool,
) -> Path:
    """Write one curated note after explicit user confirmation.

    Only sources already reviewed as reference/deep_dive are accepted.  The
    note contains metadata and links, not copied transcript bodies.
    """
    root = root.expanduser().resolve()
    title = title.strip()
    conclusion = conclusion.strip()
    rationale = rationale.strip()
    scope = scope.strip()
    confirmed_by = confirmed_by.strip()
    source_ids = list(dict.fromkeys(str(value).strip() for value in source_ids if str(value).strip()))
    if not user_confirmed:
        raise ValueError("缺少人工确认；只有用户明确确认的结论才能进入 notes/")
    if not title or not conclusion or not rationale or not confirmed_by:
        raise ValueError("标题、结论、形成理由和确认人都不能为空")
    if not source_ids:
        raise ValueError("至少需要一个已审阅来源 ID")

    catalog = _catalog(root)
    states = review_book.load_states(root / review_book.STATE_NAME)
    sources: list[dict] = []
    for video_id in source_ids:
        row = catalog.get(video_id)
        if not row:
            raise ValueError(f"索引中没有来源 {video_id}")
        state = str((states.get(video_id) or {}).get("review_status") or "pending")
        if state not in ALLOWED_SOURCE_STATES:
            raise ValueError(
                f"来源 {video_id} 的审阅状态是 {state}；"
                "只有可参考或重点深挖的来源能支持正式知识"
            )
        sources.append({"video_id": video_id, "state": state, **row})

    path = root / "notes" / _filename(title)
    if path.exists():
        raise FileExistsError(f"正式笔记已存在，拒绝静默覆盖：{path}")
    created_at = _now()
    frontmatter_ids = ", ".join(_json_string(video_id) for video_id in source_ids)
    lines = [
        "---",
        f"title: {_json_string(title)}",
        'knowledge_status: "confirmed"',
        f"confirmed_by: {_json_string(confirmed_by)}",
        f"confirmed_at: {_json_string(created_at)}",
        f"source_ids: [{frontmatter_ids}]",
        "---",
        "",
        f"# {title}",
        "",
        "## 已确认结论",
        "",
        conclusion,
        "",
        "## 形成理由",
        "",
        f"{rationale}（用户文字确认）",
        "",
        "## 适用范围",
        "",
        scope or "尚未限定；后续使用时需结合原问题复核。（用户文字确认）",
        "",
        "## 来源",
        "",
    ]
    for source in sources:
        video_id = str(source["video_id"])
        source_title = str(source.get("title") or video_id).replace("\n", " ")
        url = review_book.canonical_url(source)
        local_path = str(source.get("path") or "")
        lines.append(
            f"- [{source_title}]({url}) — `video_id={video_id}`；"
            f"审阅状态 `{source['state']}`；本地来源 `{local_path}`"
        )
    lines.extend([
        "",
        "## 溯源说明",
        "",
        "本笔记保存的是人工确认后的结论；来源作品仍是证据材料，不自动等同于事实。",
        "结论被推翻时应标记作废并记录原因，不应直接删除历史。",
        "",
    ])
    _atomic_write(path, "\n".join(lines))

    event = {
        "schema_version": 1,
        "event": "knowledge_promoted",
        "title": title,
        "path": str(path),
        "source_ids": source_ids,
        "confirmed_by": confirmed_by,
        "confirmed_at": created_at,
    }
    review_book.append_event(root / EVENTS_NAME, event)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="把人工确认的结论写入精选知识库")
    parser.add_argument("--dir", type=Path, default=DEFAULT_LIBRARY)
    parser.add_argument("--title", required=True)
    parser.add_argument("--conclusion", required=True)
    parser.add_argument("--source-id", action="append", required=True)
    parser.add_argument("--rationale", required=True)
    parser.add_argument("--scope", default="")
    parser.add_argument("--confirmed-by", required=True)
    parser.add_argument(
        "--user-confirmed",
        action="store_true",
        help="声明以上结论已经用户明确确认；缺少此项时拒绝写入",
    )
    args = parser.parse_args()
    path = promote(
        args.dir,
        title=args.title,
        conclusion=args.conclusion,
        source_ids=args.source_id,
        rationale=args.rationale,
        scope=args.scope,
        confirmed_by=args.confirmed_by,
        user_confirmed=args.user_confirmed,
    )
    print(f"已写入精选知识：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
