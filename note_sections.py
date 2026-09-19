"""Markdown 笔记区块边界。原始文件保持不变，仅为预览和检索提取正文。"""

from __future__ import annotations

import re


_TRANSCRIPT_HEADING = re.compile(r"^##\s+转写稿\s*$", re.MULTILINE)
_NEXT_LEVEL_TWO_HEADING = re.compile(r"^##\s+.+$", re.MULTILINE)
_LEVEL_TWO_SECTION = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_IMAGE_HEADING = re.compile(r"^\s*###\s*图\s*\d+\s*$")
_FOLLOW_CONTROL = re.compile(r"^\s*(?:[+＋十]\s*关注|已关注)\s*$")
_FOLLOWER_COUNT = re.compile(
    r"^\s*(?:"
    r"(?:粉丝|关注者)\s*\d+(?:\.\d+)?\s*(?:万|w|W)?|"
    r"\d+(?:\.\d+)?\s*(?:万|w|W)?\s*(?:粉丝|关注者)"
    r")\s*$"
)
_INTERACTION_COUNT = re.compile(
    r"^\s*\d+(?:\.\d+)?\s*(?:万|w|W)?\s*人?\s*"
    r"(?:赞同(?:了该回答)?|点赞|评论|收藏|转发|听过)[）)]?\s*$"
)
_EXACT_UI_PROMPT = re.compile(r"^\s*(?:问\s*AI|查看全部|展开|收起)\s*$", re.IGNORECASE)
_POSSIBLE_UI = re.compile(r"问.*AI|赞同|听过|粉丝|关注", re.IGNORECASE)
_TECHNICAL_FAILURE_PLACEHOLDER = re.compile(
    r"^\s*[（(]画面识别失败：[A-Za-z_][A-Za-z0-9_.]*[）)]\s*$"
)


def transcript_section(text: str) -> str | None:
    """返回“转写稿”区块；在下一个二级标题前停止，避免关联笔记混入。"""
    heading = _TRANSCRIPT_HEADING.search(text)
    if not heading:
        return None
    start = heading.end()
    next_heading = _NEXT_LEVEL_TWO_HEADING.search(text, start)
    end = next_heading.start() if next_heading else len(text)
    return text[start:end].strip()


def curated_knowledge_text(text: str) -> str | None:
    """Extract semantic content from generated confirmed notes.

    Source lists and traceability boilerplate remain in the Markdown file but
    are excluded from embeddings so source IDs, titles and paths do not create
    artificial impact matches.
    """
    allowed = {"已确认结论", "形成理由", "适用范围"}
    matches = list(_LEVEL_TWO_SECTION.finditer(text))
    selected: list[str] = []
    for index, match in enumerate(matches):
        title = match.group(1).strip()
        if title not in allowed:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end():end].strip()
        if body:
            selected.append(f"## {title}\n\n{body}")
    return "\n\n".join(selected) if selected else None


def is_technical_failure_placeholder(text: str) -> bool:
    """技术错误占位符不是作品正文；仅匹配整段完全等于占位符的旧稿。"""
    return bool(_TECHNICAL_FAILURE_PLACEHOLDER.fullmatch(text or ""))


def clean_screenshot_ocr(text: str) -> dict:
    """旁路清理截图 OCR；只删整行、高置信 UI 噪声，原字符串由调用方保留。

    没有 ``### 图 N`` 结构时完全不启用，避免把普通口播里的“点赞/关注”等正文删掉。
    账号名、作者、日期、标题和任何不能精确判定的行都保留。
    """
    lines = text.splitlines()
    if not any(_IMAGE_HEADING.fullmatch(line) for line in lines):
        return {"cleaned_text": text, "removed_lines": [], "flagged_lines": [], "applied": False}

    kept: list[str] = []
    removed: list[dict] = []
    flagged: list[dict] = []
    rules = (
        (_IMAGE_HEADING, "图片序号标题"),
        (_FOLLOW_CONTROL, "平台关注按钮"),
        (_FOLLOWER_COUNT, "账号粉丝/关注者数量"),
        (_INTERACTION_COUNT, "平台互动数量"),
        (_EXACT_UI_PROMPT, "平台按钮或提示"),
    )
    for number, line in enumerate(lines, 1):
        match = next(((reason, pattern) for pattern, reason in rules if pattern.fullmatch(line)), None)
        if match:
            removed.append({"line_number": number, "text": line, "reason": match[0]})
            continue
        kept.append(line)
        if line.strip() and _POSSIBLE_UI.search(line):
            flagged.append({
                "line_number": number,
                "text": line,
                "reason": "疑似平台文字，但不满足高置信整行规则，已保留",
            })

    return {
        "cleaned_text": "\n".join(kept).strip(),
        "removed_lines": removed,
        "flagged_lines": flagged,
        "applied": True,
    }
