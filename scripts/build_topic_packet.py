"""Build a review packet from DouyinNotes metadata without modifying source notes."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from note_sections import transcript_section


def extract_transcript(text: str) -> str:
    extracted = transcript_section(text)
    return extracted if extracted is not None else "（未找到转写稿段落）"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--pattern")
    selection.add_argument(
        "--ids",
        help="Comma-separated video IDs selected by an already reviewed topic scope.",
    )
    parser.add_argument("--title", required=True)
    args = parser.parse_args()

    regex = re.compile(args.pattern, re.IGNORECASE) if args.pattern else None
    requested_ids = [value.strip() for value in (args.ids or "").split(",") if value.strip()]
    indexed: dict[str, dict] = {}
    for line in args.index.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        video_id = str(item.get("video_id", ""))
        if video_id:
            indexed[video_id] = item

    if requested_ids:
        missing = [video_id for video_id in requested_ids if video_id not in indexed]
        if missing:
            raise SystemExit(f"Missing video IDs in index: {', '.join(missing)}")
        rows = [indexed[video_id] for video_id in requested_ids]
        selection_note = "筛选依据为人工核对后的明确 video_id 清单"
    else:
        rows = []
        for item in indexed.values():
            haystack = f"{item.get('title', '')} {' '.join(item.get('tags', []))}"
            if regex and regex.search(haystack):
                rows.append(item)
        rows.sort(key=lambda item: item.get("at", ""))
        selection_note = "筛选依据为标题或标签关键词"

    output = [
        f"# {args.title}",
        "",
        f"> 候选数量：{len(rows)}。{selection_note}；是否属于核心知识将在人工讨论中分层。",
        "> 本文件是讨论工作包，不等同于已经确认的知识库结论。",
        "",
        "## 完整链接清单",
        "",
    ]
    for number, item in enumerate(rows, 1):
        title = item.get("title") or "（无标题）"
        video_id = item.get("video_id", "")
        canonical_url = f"https://www.douyin.com/video/{video_id}"
        source_path = Path(item.get("path", ""))
        output.append(
            f"{number}. [{title}]({canonical_url}) — [本地原文](<{source_path.as_posix()}>)"
        )

    output.extend(["", "## 全部转写稿", ""])
    for number, item in enumerate(rows, 1):
        title = item.get("title") or "（无标题）"
        video_id = item.get("video_id", "")
        canonical_url = f"https://www.douyin.com/video/{video_id}"
        source_path = Path(item.get("path", ""))
        try:
            source_text = source_path.read_text(encoding="utf-8")
            transcript = extract_transcript(source_text)
        except OSError as exc:
            transcript = f"（读取失败：{exc}）"
        output.extend(
            [
                f"### {number}. {title}",
                "",
                f"- 抖音：{canonical_url}",
                f"- 原文：{source_path.as_posix()}",
                "- 人工摘要：待本轮逐批讨论后填写",
                "",
                transcript,
                "",
            ]
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(output), encoding="utf-8")
    print(f"Wrote {len(rows)} items to {args.output}")


if __name__ == "__main__":
    main()
