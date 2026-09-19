"""把笔记切成 chunk：聚类和检索都得在 chunk 级做，不能拿整条稿子算。

一条 20 分钟的视频可能同时讲 Agent 架构、RAG 召回、面试经验三件事，
整条打一个向量必然是浆糊。库里最长的一条 6 万字，整条 embedding 毫无意义。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from note_sections import clean_screenshot_ocr, is_technical_failure_placeholder, transcript_section

TARGET = 400      # 目标长度（字），bge-m3 上 300~500 字的块检索效果最稳
MAX_CHARS = 700   # 硬上限，超了就切
MIN_CHARS = 80    # 太短的尾巴并回上一块

_SENT_END = re.compile(r"(?<=[。！？!?])")
_IMG_HEAD = re.compile(r"^### 图 \d+", re.M)


def _front_matter(text: str) -> dict:
    meta = {}
    for key in ("title", "video_id", "source", "category", "platform", "knowledge_status"):
        m = re.search(rf'^{key}: "?(.*?)"?$', text, re.M)
        if m:
            meta[key] = m.group(1)
    m = re.search(r'^tags: \[(.*)\]', text, re.M)
    meta["tags"] = re.findall(r'"([^"]*)"', m.group(1)) if m else []
    return meta


def split_text(body: str) -> list[str]:
    """按句子边界攒块。图文稿已经有「### 图 N」小标题，那就直接照它切。"""
    if _IMG_HEAD.search(body):
        blocks = [b.strip() for b in re.split(r"^### 图 \d+\s*$", body, flags=re.M)]
        return [b for b in blocks if len(b) >= MIN_CHARS]

    sentences = [s.strip() for s in _SENT_END.split(body) if s.strip()]
    out, buf = [], ""
    for sent in sentences:
        # 单句就超上限（whisper 偶尔整段不断句）→ 硬切
        while len(sent) > MAX_CHARS:
            out.append(sent[:MAX_CHARS])
            sent = sent[MAX_CHARS:]
        if len(buf) + len(sent) > MAX_CHARS and buf:
            out.append(buf)
            buf = sent
        else:
            buf += sent
            if len(buf) >= TARGET:
                out.append(buf)
                buf = ""
    if buf:
        if out and len(buf) < MIN_CHARS:
            out[-1] += buf   # 太短的尾巴并回去，别留半句话的孤块
        else:
            out.append(buf)
    return out


def chunk_file(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    meta = _front_matter(text)
    extracted = transcript_section(text)
    body = extracted if extracted is not None else text
    # 截图 OCR 采用旁路清洗：检索使用清洗文本，原 Markdown 永不改写。
    body = clean_screenshot_ocr(body)["cleaned_text"]
    # 旧稿可能把 OCR 异常名写进转写章节；技术占位符绝不能进入知识库。
    if is_technical_failure_placeholder(body):
        body = ""
    # 正文顶部那行「🔗 在抖音打开原视频」和引用的 desc 不是内容，去掉
    body = re.sub(r"^🔗 .*$", "", body, flags=re.M)
    body = re.sub(r"^> .*$", "", body, flags=re.M).strip()
    rows = []
    for i, piece in enumerate(split_text(body)):
        rows.append({
            "chunk_id": f'{meta.get("video_id", path.stem)}#{i}',
            "video_id": meta.get("video_id", ""),
            "title": meta.get("title", ""),
            "category": meta.get("category", ""),
            "tags": meta.get("tags", []),
            "source": meta.get("source", ""),
            "knowledge_status": meta.get("knowledge_status", ""),
            "path": str(path),
            "idx": i,
            "text": piece,
        })
    return rows


def chunk_library(root: Path) -> list[dict]:
    review_states: dict[str, str] = {}
    review_path = root / "_审阅状态.jsonl"
    if review_path.is_file():
        with review_path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                video_id = str(event.get("video_id") or "")
                status = str(event.get("review_status") or "")
                if video_id and status:
                    review_states[video_id] = status
    tombstones: set[str] = set()
    tombstone_path = root / "_删除标记.jsonl"
    if tombstone_path.is_file():
        with tombstone_path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    marker = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if marker.get("status") == "deleted" and marker.get("video_id"):
                    tombstones.add(str(marker["video_id"]))

    rows = []
    for sub in ("inbox", "notes"):
        for f in sorted((root / sub).rglob("*.md")):
            file_rows = chunk_file(f)
            if not file_rows:
                continue
            video_id = str(file_rows[0].get("video_id") or "")
            source_layer = "curated" if sub == "notes" else "material"
            review_status = (
                str(file_rows[0].get("knowledge_status") or "confirmed")
                if source_layer == "curated"
                else review_states.get(video_id, "pending")
            )
            if review_status == "exclude" or video_id in tombstones:
                continue
            for row in file_rows:
                row["review_status"] = review_status
                row["source_layer"] = source_layer
            rows.extend(file_rows)
    return rows
