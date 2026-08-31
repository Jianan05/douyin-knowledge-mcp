"""抖音收藏的视觉资产保存与本地分流。

这里只做本地计算：OpenCV 抽镜头、RapidOCR 读画面、Whisper 估计语音信息密度。
分类是“推断，未证实”，必须经过用户校准；本模块不负责取消收藏。
"""

from __future__ import annotations

import json
import math
import re
import shutil
import time
from pathlib import Path


TUTORIAL_WORDS = {
    "教程", "教学", "剪辑", "剪映", "模板", "转场", "特效", "调色", "蒙版",
    "关键帧", "时间轴", "pr", "premiere", "ae", "after effects", "capcut",
    "达芬奇", "davinci", "参数", "步骤", "操作",
}
VISUAL_WORDS = {
    "ai视频", "ai生成", "aigc", "视觉", "运镜", "镜头", "画面", "风格", "动画",
    "cg", "建模", "渲染", "可灵", "即梦", "sora", "runway", "特效", "短片",
}
SPOKEN_WORDS = {
    "观点", "认知", "分析", "解读", "访谈", "演讲", "经验", "方法", "为什么",
    "如何", "知识", "逻辑", "复盘", "建议", "思考",
}
UI_WORDS = {
    "文件", "编辑", "视图", "导出", "项目", "序列", "时间轴", "效果", "预览",
    "图层", "蒙版", "关键帧", "不透明度", "混合模式", "轨道", "素材", "渲染",
}


def _safe_name(text: str, limit: int = 70) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text or "").strip(" ._")
    return text[:limit] or "未命名"


def _stamp(seconds: float) -> str:
    ms = int(round(max(seconds, 0) * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}-{m:02d}-{s:02d}.{milli:03d}"


def _atomic_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def extract_scene_frames(
    video_path: str, every_sec: float = 1.5, threshold: float = 7.0,
    max_saved: int = 24,
) -> tuple[list[dict], dict]:
    """固定间隔扫描，但只保留变化明显的帧；返回带时间戳的代表镜头。"""
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(video_path)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = total_frames / fps if total_frames else 0.0
    sample_count = max(1, int(math.ceil(duration / every_sec)))
    candidates: list[dict] = []
    prev = None
    diffs: list[float] = []
    change_count = 0

    for n in range(sample_count + 1):
        second = min(n * every_sec, max(duration - 0.05, 0))
        cap.set(cv2.CAP_PROP_POS_MSEC, second * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (64, 64))
        small_i = small.astype("int16")
        diff = 100.0 if prev is None else float(np.abs(small_i - prev).mean())
        diffs.append(diff)
        if prev is None or diff >= threshold:
            change_count += int(prev is not None)
            candidates.append({"second": round(second, 3), "frame": frame, "diff": round(diff, 2)})
        prev = small_i
    cap.release()

    if len(candidates) > max_saved:
        indexes = {
            round(i * (len(candidates) - 1) / (max_saved - 1))
            for i in range(max_saved)
        }
        candidates = [row for i, row in enumerate(candidates) if i in indexes]

    stats = {
        "duration_s": round(duration, 2),
        "sample_interval_s": every_sec,
        "sampled_frames": len(diffs),
        "scene_changes": change_count,
        "scene_changes_per_min": round(change_count / max(duration / 60, 0.1), 2),
        "mean_frame_difference": round(sum(diffs[1:]) / max(len(diffs) - 1, 1), 2),
    }
    return candidates, stats


def save_frames(rows: list[dict], directory: Path) -> list[dict]:
    import cv2

    directory.mkdir(parents=True, exist_ok=True)
    saved = []
    for idx, row in enumerate(rows, 1):
        name = f"{idx:03d}_{_stamp(row['second'])}.jpg"
        path = directory / name
        cv2.imwrite(str(path), row["frame"], [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        saved.append({
            "index": idx,
            "second": row["second"],
            "difference": row["diff"],
            "path": str(path),
        })
    return saved


def make_contact_sheet(rows: list[dict], path: Path, columns: int = 4) -> None:
    """代表帧拼成一张图；视觉模型只需看这一张，而不是逐帧传图。"""
    from PIL import Image, ImageDraw

    if not rows:
        return
    picked = rows
    if len(rows) > 12:
        idx = {round(i * (len(rows) - 1) / 11) for i in range(12)}
        picked = [row for i, row in enumerate(rows) if i in idx]

    cell_w, cell_h, label_h = 360, 203, 24
    lines = math.ceil(len(picked) / columns)
    sheet = Image.new("RGB", (columns * cell_w, lines * (cell_h + label_h)), "#111111")
    draw = ImageDraw.Draw(sheet)
    for i, row in enumerate(picked):
        frame = row["frame"][:, :, ::-1]
        img = Image.fromarray(frame).convert("RGB")
        img.thumbnail((cell_w, cell_h))
        x = (i % columns) * cell_w
        y = (i // columns) * (cell_h + label_h)
        sheet.paste(img, (x + (cell_w - img.width) // 2, y + (cell_h - img.height) // 2))
        draw.text((x + 6, y + cell_h + 4), _stamp(row["second"]), fill="white")
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path, format="JPEG", quality=86, optimize=True)


def ocr_frame_signals(rows: list[dict], max_frames: int = 12) -> dict:
    import image_note
    import frames as frame_tools

    engine = image_note._engine()
    texts: list[str] = []
    for row in rows[:max_frames]:
        texts.extend(frame_tools._ocr_frame(engine, row["frame"]))
    unique = list(dict.fromkeys(texts))
    joined = "\n".join(unique)
    lowered = joined.lower()
    ui_hits = sorted(w for w in UI_WORDS if w.lower() in lowered)
    return {
        "ocr_unique_lines": len(unique),
        "ocr_chars": len(joined.replace("\n", "").replace(" ", "")),
        "ocr_chars_per_frame": round(len(joined) / max(min(len(rows), max_frames), 1), 1),
        "ui_keyword_hits": ui_hits,
        "text": joined,
    }


def classify(item: dict, signals: dict) -> dict:
    """可解释的保守分类；证据和分数都落盘，方便用户校准。"""
    desc = (item.get("desc") or "").lower()
    ocr = (signals.get("ocr_text") or "").lower()
    all_text = desc + "\n" + ocr
    tutorial_hits = sorted(w for w in TUTORIAL_WORDS if w.lower() in all_text)
    visual_hits = sorted(w for w in VISUAL_WORDS if w.lower() in all_text)
    spoken_hits = sorted(w for w in SPOKEN_WORDS if w.lower() in all_text)

    cpm = float(signals.get("speech_chars_per_min") or 0)
    scene_rate = float(signals.get("scene_changes_per_min") or 0)
    ui_hits = signals.get("ui_keyword_hits") or []
    scores = {
        "spoken_knowledge": min(4.0, cpm / 90) + min(2.0, len(spoken_hits) * 0.7),
        "visual_reference": min(3.0, scene_rate / 5) + min(3.0, len(visual_hits) * 0.9),
        "editing_tutorial": min(4.0, len(tutorial_hits) * 0.9) + min(3.0, len(ui_hits) * 0.8),
    }
    if cpm < 60:
        scores["visual_reference"] += 1.0
    if cpm >= 180 and scene_rate < 5:
        scores["spoken_knowledge"] += 1.0

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top_name, top_score = ranked[0]
    second_name, second_score = ranked[1]
    mixed = top_score >= 3.0 and second_score >= 2.7 and top_score - second_score < 1.2
    primary = "mixed" if mixed else top_name
    margin = max(top_score - second_score, 0)
    confidence = min(0.94, 0.42 + top_score * 0.06 + margin * 0.09)
    if top_score < 1.8:
        primary, confidence = "unknown", min(confidence, 0.48)

    evidence = [
        f"语音文字密度 {cpm:.0f} 字/分钟",
        f"镜头变化 {scene_rate:.1f} 次/分钟",
        f"画面 OCR {signals.get('ocr_chars', 0)} 字",
    ]
    if tutorial_hits:
        evidence.append("教程/剪辑词：" + "、".join(tutorial_hits[:8]))
    if visual_hits:
        evidence.append("视觉词：" + "、".join(visual_hits[:8]))
    if ui_hits:
        evidence.append("软件界面词：" + "、".join(ui_hits[:8]))
    return {
        "primary_value": primary,
        "confidence": round(confidence, 2),
        "scores": {k: round(v, 2) for k, v in scores.items()},
        "evidence": evidence,
        "classification_source": "本地元数据+Whisper字数密度+OpenCV镜头变化+RapidOCR（推断，未证实）",
        "review_status": "unverified",
    }


def process_video(
    item: dict, video_path: str, assets_root: Path, transcript: str,
    keep_source: bool = True,
) -> dict:
    aweme_id = item["aweme_id"]
    asset_dir = assets_root / aweme_id
    frame_dir = asset_dir / "frames"
    asset_dir.mkdir(parents=True, exist_ok=True)
    rows, stats = extract_scene_frames(video_path)
    saved = save_frames(rows, frame_dir)
    contact = asset_dir / "contact-sheet.jpg"
    make_contact_sheet(rows, contact)
    ocr = ocr_frame_signals(rows)
    duration_min = max(stats["duration_s"] / 60, 0.1)
    signals = {
        **stats,
        "ocr_unique_lines": ocr["ocr_unique_lines"],
        "ocr_chars": ocr["ocr_chars"],
        "ocr_chars_per_frame": ocr["ocr_chars_per_frame"],
        "ui_keyword_hits": ocr["ui_keyword_hits"],
        "ocr_text": ocr["text"],
        "speech_chars": len((transcript or "").replace("\n", "").replace(" ", "")),
        "speech_chars_per_min": round(
            len((transcript or "").replace("\n", "").replace(" ", "")) / duration_min, 1
        ),
    }
    decision = classify(item, signals)
    source_path = ""
    if keep_source:
        source = asset_dir / "source.mp4"
        shutil.copy2(video_path, source)
        source_path = str(source)
    (asset_dir / "screen-ocr.txt").write_text(ocr["text"], encoding="utf-8")
    manifest = {
        "schema": 1,
        "aweme_id": aweme_id,
        "title": item.get("desc") or "",
        "source_url": item.get("url") or "",
        "kind": "video",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_media": source_path,
        "contact_sheet": str(contact) if contact.exists() else "",
        "frames": saved,
        "signals": {k: v for k, v in signals.items() if k != "ocr_text"},
        "classification": decision,
        "asset_status": "calibration_complete",
        "safe_to_uncollect": False,
    }
    _atomic_json(asset_dir / "manifest.json", manifest)
    return manifest


def _image_extension(data: bytes) -> str:
    from PIL import Image
    import io

    try:
        fmt = (Image.open(io.BytesIO(data)).format or "JPEG").lower()
    except Exception:
        return ".bin"
    return {"jpeg": ".jpg", "png": ".png", "webp": ".webp", "gif": ".gif"}.get(fmt, f".{fmt}")


def process_image_post(item: dict, assets_root: Path) -> dict:
    import image_note
    from PIL import Image

    aweme_id = item["aweme_id"]
    asset_dir = assets_root / aweme_id
    originals = asset_dir / "originals"
    originals.mkdir(parents=True, exist_ok=True)
    images = []
    ocr_parts = []
    pil_rows = []
    for idx, url in enumerate(item.get("images") or [], 1):
        data = image_note.fetch_image(url)
        path = originals / f"{idx:02d}{_image_extension(data)}"
        path.write_bytes(data)
        text = image_note.ocr_bytes(data)
        ocr_parts.append(f"### 图 {idx}\n\n{text}".strip())
        images.append({"index": idx, "path": str(path), "ocr_chars": len(text)})
        pil_rows.append(Image.open(path).convert("RGB"))

    contact = asset_dir / "contact-sheet.jpg"
    if pil_rows:
        thumbs = []
        for img in pil_rows[:12]:
            copy = img.copy()
            copy.thumbnail((360, 360))
            thumbs.append(copy)
        cols = 4
        rows_n = math.ceil(len(thumbs) / cols)
        sheet = Image.new("RGB", (cols * 360, rows_n * 360), "white")
        for i, img in enumerate(thumbs):
            sheet.paste(img, ((i % cols) * 360 + (360 - img.width) // 2,
                              (i // cols) * 360 + (360 - img.height) // 2))
        sheet.save(contact, "JPEG", quality=86, optimize=True)

    ocr_text = "\n\n".join(ocr_parts)
    (asset_dir / "ocr.txt").write_text(ocr_text, encoding="utf-8")
    dense = len(ocr_text) / max(len(images), 1) >= 80
    primary = "image_text" if dense else "visual_reference"
    manifest = {
        "schema": 1,
        "aweme_id": aweme_id,
        "title": item.get("desc") or "",
        "source_url": item.get("url") or "",
        "kind": "image",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "originals": images,
        "contact_sheet": str(contact) if contact.exists() else "",
        "classification": {
            "primary_value": primary,
            "confidence": 0.72 if dense else 0.58,
            "evidence": [f"原图 {len(images)} 张", f"OCR {len(ocr_text)} 字"],
            "classification_source": "原图数量+RapidOCR文字密度（推断，未证实）",
            "review_status": "unverified",
        },
        "asset_status": "calibration_complete",
        "safe_to_uncollect": False,
    }
    _atomic_json(asset_dir / "manifest.json", manifest)
    return manifest


def write_calibration_report(manifests: list[dict], root: Path) -> Path:
    labels = {
        "spoken_knowledge": "口播知识",
        "visual_reference": "视觉参考",
        "editing_tutorial": "剪辑/软件教学",
        "image_text": "图文文字",
        "mixed": "混合内容",
        "unknown": "不确定",
    }
    out_dir = root / "视觉校准"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"校准-{time.strftime('%Y%m%d-%H%M%S')}.md"
    lines = [
        "# 抖音收藏视觉校准", "",
        "> 所有分类均为本地特征推断，未证实。请按真实收藏原因纠正；校准期间不取消收藏。", "",
    ]
    for idx, manifest in enumerate(manifests, 1):
        decision = manifest["classification"]
        title = _safe_name(manifest.get("title") or manifest["aweme_id"], 100)
        contact = Path(manifest.get("contact_sheet") or "")
        rel = contact.relative_to(root).as_posix() if contact.is_file() else ""
        lines.extend([
            f"## {idx:02d}. {title}", "",
            f"![[{rel}]]" if rel else "（没有生成分镜图）", "",
            f"- 初判：**{labels.get(decision['primary_value'], decision['primary_value'])}** "
            f"（置信度 {decision['confidence']:.0%}，推断，未证实）",
            "- 依据：" + "；".join(decision.get("evidence") or []),
            f"- 原作品：[打开抖音]({manifest.get('source_url', '')})",
            "- 你的纠正：收藏原因 = ；应保存 = ；这个判断对/错 =", "",
        ])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
