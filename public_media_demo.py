"""Run a private-data-free demo through real local ASR, OCR, and text inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import image_note
import ingest
import knowledge_notes
import review_book


ASSET_DIR = PROJECT_DIR / "examples" / "public-demo"
AUDIO_ID = "9000000000000000101"
IMAGE_ID = "9000000000000000102"
TEXT_ID = "9000000000000000103"
AUDIO_SOURCE = "https://commons.wikimedia.org/wiki/File:The_English_word_EXAMPLE.wav"


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


def create_demo(root: Path, model: str = "tiny") -> dict:
    root = root.resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"演示目录必须为空，拒绝覆盖：{root}")
    required = ("speech.wav", "text-card.png", "text-post.txt")
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

    audio_transcript = server._transcribe_segments_sync(
        str(copied["speech.wav"]), model
    )
    audio_meta = {
        "video_id": AUDIO_ID,
        "title": "CC0 public audio fixture",
        "url": AUDIO_SOURCE,
        "platform": "public-demo-audio",
        "tags": ["public-demo", "audio", "CC0"],
        "duration": float(audio_transcript.asr.get("duration") or 0),
    }
    source_package = lib.write_source_package(audio_meta, audio_transcript)
    audio_note = _record_item(
        lib,
        video_id=AUDIO_ID,
        title=audio_meta["title"],
        source=AUDIO_SOURCE,
        kind="audio",
        transcript=audio_transcript,
        model=model,
        device=str(audio_transcript.asr.get("device") or "auto"),
        asset_path=copied["speech.wav"],
        duration=audio_meta["duration"],
        source_package=source_package,
    )

    ocr_text = image_note.ocr_bytes(copied["text-card.png"].read_bytes())
    if not ocr_text.strip():
        raise RuntimeError("公开图片夹具未识别出任何文字")
    image_transcript = "### 图 1\n\n" + ocr_text.strip()
    image_note_path = _record_item(
        lib,
        video_id=IMAGE_ID,
        title="Generated public OCR fixture",
        source="fixture://public-demo/text-card.png",
        kind="image",
        transcript=image_transcript,
        model="rapidocr",
        device="CPU",
        asset_path=copied["text-card.png"],
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

    review_book.prepare_batch(root, limit=3)
    for source_id in (AUDIO_ID, IMAGE_ID, TEXT_ID):
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
            "音频转录、图片 OCR 和纯文字都可以进入同一套可追溯素材结构；"
            "自动提取结果仍须经过人工审阅，才能提升为长期知识。"
        ),
        source_ids=[AUDIO_ID, IMAGE_ID, TEXT_ID],
        rationale=(
            "公开演示实际运行本地 Whisper、RapidOCR 和直接文字入库，"
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
                "source_id": AUDIO_ID,
                "kind": "audio",
                "origin": AUDIO_SOURCE,
                "license": "CC0-1.0",
                "sha256": _sha256(copied["speech.wav"]),
                "extracted_text": str(audio_transcript),
                "note": str(audio_note),
                "source_package": str(source_package or ""),
            },
            {
                "source_id": IMAGE_ID,
                "kind": "image",
                "origin": "project-generated fixture",
                "license": "MIT",
                "sha256": _sha256(copied["text-card.png"]),
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
    return {
        "root": str(root),
        "manifest": str(manifest_path),
        "knowledge_note": str(knowledge_note),
        "audio_text": str(audio_transcript),
        "ocr_text": ocr_text,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="运行不含私人数据的真实音频、图片 OCR 与文字知识闭环"
    )
    parser.add_argument("--output", type=Path, help="空目录；省略时使用系统临时目录")
    parser.add_argument("--model", default="tiny", help="Whisper 模型，默认 tiny")
    args = parser.parse_args()
    output = args.output or Path(tempfile.mkdtemp(prefix="douyin-public-media-demo-"))
    print(json.dumps(create_demo(output, args.model), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
