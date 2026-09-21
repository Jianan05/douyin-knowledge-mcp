"""Run a private-data-free demo through real local ASR, OCR, and text inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Callable

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import image_note
import ingest
import knowledge_notes
import review_book


ASSET_DIR = PROJECT_DIR / "examples" / "public-demo"
VIDEO_ID = "9000000000000000101"
IMAGE_ID = "9000000000000000102"
TEXT_ID = "9000000000000000103"
VIDEO_SOURCE = "https://commons.wikimedia.org/wiki/File:2009-07-04_President_Obama%27s_Weekly_Address.ogv"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _record_item(
    lib: ingest.Library,
    *,
    video_id: str,
    title: str,
    source: str,
    kind: str,
    transcript: str,
    model: str,
    device: str,
    asset_path: Path,
    duration: float = 0,
    source_package: Path | None = None,
) -> Path:
    meta = {
        "video_id": video_id,
        "title": title,
        "url": source,
        "platform": f"public-demo-{kind}",
        "tags": ["public-demo", kind],
        "duration": round(duration, 3),
        "chars": len(str(transcript).replace("\n", "").replace(" ", "")),
        "model": model,
        "device": device,
        "source_package": (
            str(source_package.relative_to(lib.root)) if source_package else ""
        ),
    }
    note = lib.write_note(meta, transcript)
    lib.record({
        "video_id": video_id,
        "url": source,
        "title": title,
        "tags": meta["tags"],
        "path": str(note),
        "asset_path": str(asset_path),
        "status": "raw",
        "kind": kind,
        "chars": meta["chars"],
        "duration": meta["duration"],
        "source_package": meta["source_package"],
    })
    return note


def create_demo(
    root: Path,
    model: str = "tiny",
    progress: Callable[[str], None] | None = None,
) -> dict:
    report = progress or (lambda _message: None)
    root = root.resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"演示目录必须为空，拒绝覆盖：{root}")
    required = ("source-clip.mp4", "title-card.png", "text-post.txt")
    for name in required:
        if not (ASSET_DIR / name).is_file():
            raise FileNotFoundError(f"缺少公开演示夹具 examples/public-demo/{name}")

    lib = ingest.Library(root)
    output_assets = root / "assets" / "public-demo"
    output_assets.mkdir(parents=True, exist_ok=True)
    copied = {
        name: Path(shutil.copy2(ASSET_DIR / name, output_assets / name))
        for name in required
    }

    # Lazy import keeps --help and unit tests free from Whisper model loading.
    import server

    report("[1/4] 本地 Whisper 转录 25 秒真人公开视频……")
    video_transcript = server._transcribe_segments_sync(
        str(copied["source-clip.mp4"]), model
    )
    video_meta = {
        "video_id": VIDEO_ID,
        "title": "President Obama's Weekly Address — July 4, 2009",
        "url": VIDEO_SOURCE,
        "platform": "public-demo-video",
        "tags": ["public-demo", "video", "public-domain"],
        "duration": float(video_transcript.asr.get("duration") or 0),
    }
    source_package = lib.write_source_package(video_meta, video_transcript)
    video_note = _record_item(
        lib,
        video_id=VIDEO_ID,
        title=video_meta["title"],
        source=VIDEO_SOURCE,
        kind="video",
        transcript=video_transcript,
        model=model,
        device=str(video_transcript.asr.get("device") or "auto"),
        asset_path=copied["source-clip.mp4"],
        duration=video_meta["duration"],
        source_package=source_package,
    )

    report("[2/4] RapidOCR 读取视频原始片头帧……")
    ocr_text = image_note.ocr_bytes(copied["title-card.png"].read_bytes())
    if not ocr_text.strip():
        raise RuntimeError("公开图片夹具未识别出任何文字")
    image_transcript = "### 图 1\n\n" + ocr_text.strip()
    image_note_path = _record_item(
        lib,
        video_id=IMAGE_ID,
        title="Weekly Address title card",
        source=VIDEO_SOURCE,
        kind="image",
        transcript=image_transcript,
        model="rapidocr",
        device="CPU",
        asset_path=copied["title-card.png"],
    )

    text_body = copied["text-post.txt"].read_text(encoding="utf-8").strip()
    text_note = _record_item(
        lib,
        video_id=TEXT_ID,
        title="Generated public text fixture",
        source="fixture://public-demo/text-post.txt",
        kind="text",
        transcript=text_body,
        model="direct-text",
        device="none",
        asset_path=copied["text-post.txt"],
    )

    report("[3/4] 写入来源、索引和人工审阅状态……")
    review_book.prepare_batch(root, limit=3)
    for source_id in (VIDEO_ID, IMAGE_ID, TEXT_ID):
        review_book.set_status(
            root,
            source_id,
            "可参考",
            "公开演示夹具已预置人工审阅结果；不代表外部事实核验",
        )
    knowledge_note = knowledge_notes.promote(
        root,
        title="混合媒体进入长期知识前必须保留证据与审阅状态",
        conclusion=(
            "真人视频转录、原始片头帧 OCR 和人工文字都可以进入同一套可追溯素材结构；"
            "自动提取结果仍须经过人工审阅，才能提升为长期知识。"
        ),
        source_ids=[VIDEO_ID, IMAGE_ID, TEXT_ID],
        rationale=(
            "公开演示实际运行真人视频的本地 Whisper、原始片头帧 RapidOCR 和直接文字入库，"
            "并通过相同审阅门槛生成确认笔记。"
        ),
        scope="仅验证本项目公开演示链路，不评价第三方内容或模型准确率。",
        confirmed_by="public-media-demo-fixture",
        user_confirmed=True,
    )

    manifest = {
        "schema_version": 1,
        "demo": "public-mixed-media",
        "private_data_used": False,
        "model": model,
        "sources": [
            {
                "source_id": VIDEO_ID,
                "kind": "video",
                "origin": VIDEO_SOURCE,
                "license": "Public-Domain-Mark-1.0",
                "sha256": _sha256(copied["source-clip.mp4"]),
                "extracted_text": str(video_transcript),
                "note": str(video_note),
                "source_package": str(source_package or ""),
            },
            {
                "source_id": IMAGE_ID,
                "kind": "image",
                "origin": "frame from public-domain source video",
                "license": "Public-Domain-Mark-1.0",
                "sha256": _sha256(copied["title-card.png"]),
                "extracted_text": ocr_text,
                "note": str(image_note_path),
            },
            {
                "source_id": TEXT_ID,
                "kind": "text",
                "origin": "project-generated fixture",
                "license": "MIT",
                "sha256": _sha256(copied["text-post.txt"]),
                "extracted_text": text_body,
                "note": str(text_note),
            },
        ],
        "knowledge_note": str(knowledge_note),
    }
    manifest_path = root / "public-demo-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report("[4/4] 生成确认知识笔记和公开演示清单。")
    return {
        "root": str(root),
        "manifest": str(manifest_path),
        "knowledge_note": str(knowledge_note),
        "video_text": str(video_transcript),
        "ocr_text": ocr_text,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="运行不含私人数据的真人视频、图片 OCR 与文字知识闭环"
    )
    parser.add_argument("--output", type=Path, help="空目录；省略时使用系统临时目录")
    parser.add_argument("--model", default="tiny", help="Whisper 模型，默认 tiny")
    parser.add_argument(
        "--brief",
        action="store_true",
        help="只打印适合公开录屏的结果摘要，不显示本机绝对路径",
    )
    args = parser.parse_args()
    output = args.output or Path(tempfile.mkdtemp(prefix="douyin-public-media-demo-"))
    result = create_demo(output, args.model, progress=lambda text: print(text, flush=True))
    if args.brief:
        print("\nASR 结果：")
        print(result["video_text"])
        print("\nOCR 结果：")
        print(result["ocr_text"])
        print("\n完成：3 条来源 → 人工审阅 → 1 条 confirmed 知识笔记")
        print("所有输出已写入临时演示库；未读取 Cookie 或私人收藏。")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
