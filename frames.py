"""抽帧 OCR：把转写稿漏掉的东西从画面里捞出来。

两个用途，输出也是两份：

1. **画面文字全文** —— 救「一张照片发成视频」「纯演示无人声」那类。
   这类视频转写稿是空的，内容全在画面上，而且往往是中文。
2. **英文候选名** —— 救「只说『这个项目』，从不念名字」那类。
   画面上是正确拼写，顺便能治 whisper 把 LangChain 听成 Longchain。

不需要判断"名字在第几帧"：全抽，靠统计筛 —— 名字会在画面上停留几秒
（跨几十帧重复出现），路人文字只出现一两帧。
"""

from __future__ import annotations

import collections
import re

# 画面上到处都是的 UI 文字 / 通用词，找项目名时一票否决
NOISE = {w.lower() for w in """
github http https www com cn org io net app apps agent skill token prompt
ai api llm gpt mcp rag cpu gpu ide sdk url npm pip git vs code json py exe md
deepseek openai claude codex cursor python java the and for you all new now
follow like share view play pause live vip hd 4k 60fps
repositoryoftheweek repositoryoftheday trending star fork issues
""".split()}

_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-\.]{2,}")
# 一行里中文/英文/数字太少就是噪声（水印花纹、界面碎字）
_MEANINGFUL = re.compile(r"[\u4e00-\u9fff]|[A-Za-z]{3,}")


def grab_frames(path: str, every_sec: float = 2.5, cap: int = 80, diff_skip: bool = True):
    """按固定间隔抽帧。

    ⚠️ 传进来的必须是**带画面**的文件。用 server._download_transcription_media
    下的是纯音频流，opencv 一帧也抽不出来（踩过，CPU 空转 37 分钟）。

    diff_skip: 跳过跟上一张几乎一样的帧。单图视频画面基本不变，
    开了之后 80 帧能压到 1~2 帧，OCR 成本几乎归零。
    """
    import cv2
    import numpy as np

    video = cv2.VideoCapture(path)
    fps = video.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(video.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    step = max(int(fps * every_sec), 1)
    picks = list(range(0, total, step))[:cap] or [0]

    out, prev = [], None
    for idx in picks:
        video.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = video.read()
        if not ok:
            continue
        if diff_skip:
            small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (64, 64))
            if prev is not None and float(np.abs(small.astype("int16") - prev).mean()) < 6.0:
                continue
            prev = small.astype("int16")
        out.append(frame)
    video.release()
    return out


def _is_garbage(text: str) -> bool:
    """滤掉水印和装饰元素被误读出来的碎片。

    实测捞出来的噪声长这样：fet、32弄、图一新113章1、Datet、�。
    共同点是**很短，或者汉字/字母占比低**（夹着一堆数字和符号）。
    真正的画面文字是成句的标题和字幕，这两条判据都过得去。
    """
    if "�" in text:
        return True
    real = sum(1 for ch in text if "一" <= ch <= "鿿" or ch.isalpha())
    if len(text) <= 3 and real < len(text):
        return True
    return real < len(text) * 0.7


def _ocr_frame(engine, frame) -> list[str]:
    result, _ = engine(frame)
    lines = []
    for line in result or []:
        if len(line) > 1:
            text = str(line[1]).strip()
            if text and _MEANINGFUL.search(text) and not _is_garbage(text):
                lines.append(text)
    return lines


def read_screen(path: str, **kw) -> tuple[str, collections.Counter]:
    """抽帧 + OCR，返回 (画面文字全文, 英文串出现帧数统计)。

    全文按**首次出现顺序**去重：同一句话在几十帧里反复出现，只留一次，
    但顺序保持跟视频推进一致，读起来还是连贯的。
    """
    import image_note

    frames = grab_frames(path, **kw)
    engine = image_note._engine()
    seen_lines: dict[str, None] = {}
    word_frames = collections.Counter()
    for frame in frames:
        lines = _ocr_frame(engine, frame)
        words = set()
        for text in lines:
            seen_lines.setdefault(text, None)
            words.update(_NAME_RE.findall(text))
        word_frames.update(words)
    return "\n".join(seen_lines), word_frames


def candidates(word_frames: collections.Counter, transcript: str = "", top: int = 8):
    """英文候选名：排掉噪声词和转写稿里已经有的词，按出现帧数排。"""
    spoken = {w.lower() for w in _NAME_RE.findall(transcript or "")}
    rows = [
        (w, n) for w, n in word_frames.items()
        if n >= 2 and w.lower() not in NOISE and w.lower() not in spoken
    ]
    rows.sort(key=lambda r: (-r[1], -len(r[0])))
    return rows[:top]
