"""图文帖 → 文字：下载图片，逐张 OCR，拼成一份稿子。

抖音图文的正文基本全在图片上（长截图、观点卡片），所以不 OCR 等于没存。
引擎用 RapidOCR（onnxruntime，纯 CPU），跟 whisper 那条链没有交集。
"""

from __future__ import annotations

import io
import urllib.request
from functools import lru_cache

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@lru_cache(maxsize=1)
def _engine():
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR()


def fetch_image(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Referer": "https://www.douyin.com/"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def ocr_bytes(data: bytes) -> str:
    """一张图 → 文字。RapidOCR 返回 (结果, 耗时)，结果是 [框, 文字, 置信度]。"""
    import numpy as np
    from PIL import Image

    img = Image.open(io.BytesIO(data)).convert("RGB")
    result, _ = _engine()(np.array(img))
    if not result:
        return ""
    return "\n".join(str(line[1]).strip() for line in result if len(line) > 1)


def ocr_images(urls: list[str]) -> tuple[str, int]:
    """多张图 → 一份稿子。返回 (正文, 成功识别的图片数)。"""
    chunks, done = [], 0
    for idx, url in enumerate(urls, 1):
        try:
            text = ocr_bytes(fetch_image(url))
        except Exception as exc:
            chunks.append(f"### 图 {idx}\n\n（识别失败：{type(exc).__name__}）")
            continue
        done += 1
        chunks.append(f"### 图 {idx}\n\n" + (text or "（这张图没识别出文字）"))
    return "\n\n".join(chunks), done
