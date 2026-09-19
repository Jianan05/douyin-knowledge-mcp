"""Run a private-data-free demo of review -> confirmed knowledge promotion."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import knowledge_notes
import review_book


DEMO_ID = "9000000000000000001"


def create_demo(root: Path) -> dict:
    root = root.resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"演示目录必须为空，拒绝覆盖：{root}")
    inbox = root / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    source = inbox / f"demo_{DEMO_ID}.md"
    source.write_text(
        "\n".join([
            "---",
            'title: "公开合成示例：证据与结论分层"',
            'platform: "demo"',
            f'video_id: "{DEMO_ID}"',
            'source: "https://example.invalid/public-demo"',
            'tags: ["知识工作流", "公开演示"]',
            "---",
            "",
            "## 转写稿",
            "",
            "这是一段项目自带的合成材料，不来自真实用户收藏。",
            "它只用于演示：原始材料经过人工审阅后，才能支持长期知识结论。",
            "",
        ]),
        encoding="utf-8",
    )
    index_row = {
        "video_id": DEMO_ID,
        "url": "https://example.invalid/public-demo",
        "title": "公开合成示例：证据与结论分层",
        "tags": ["知识工作流", "公开演示"],
        "path": str(source),
        "status": "raw",
        "chars": 58,
        "duration": 0,
    }
    (root / "index.jsonl").write_text(
        json.dumps(index_row, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    review_book.prepare_batch(root, limit=1)
    review_book.set_status(root, DEMO_ID, "可参考", "公开合成示例已人工预置审阅结果")
    note = knowledge_notes.promote(
        root,
        title="证据材料与长期知识必须分层",
        conclusion="自动提取和检索得到的是证据候选；只有人工确认后的结论才进入长期知识库。",
        source_ids=[DEMO_ID],
        rationale="这是项目公开演示中预置的确认步骤，用于验证状态门槛和来源追溯。",
        scope="仅描述本项目的知识入库规则，不代表对外部事实的判断。",
        confirmed_by="public-demo-fixture",
        user_confirmed=True,
    )
    return {
        "root": str(root),
        "source": str(source),
        "review_book": str(root / review_book.BOOK_NAME),
        "knowledge_note": str(note),
        "events": str(root / knowledge_notes.EVENTS_NAME),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="运行不含私人数据的知识闭环演示")
    parser.add_argument("--output", type=Path, help="空目录；省略时使用系统临时目录")
    args = parser.parse_args()
    output = args.output or Path(tempfile.mkdtemp(prefix="douyin-knowledge-demo-"))
    result = create_demo(output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
