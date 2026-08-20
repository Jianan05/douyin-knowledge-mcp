"""
批量入库：抖音/Bilibili 链接 → 本地转录 → 带 frontmatter 的 .md → 去重归档。

设计目标是**省 token**：转录稿只落磁盘，不进对话。所以这个脚本
stdout 每条视频只打一行摘要（状态 / ID / 标题 / 时长 / 字数 / 路径），
正文一个字都不往外打。

分类**故意先不做**：抓到的 `#话题标签` 原样存进 frontmatter 的 tags，
category 留空，文件全进 inbox/。等攒够几十条，再按真实的标题和标签
聚类出 categories.toml，那时候一次性归档，比现在拍脑袋猜准得多。

用法：
    python ingest.py <链接或分享文本> [更多链接...]
    python ingest.py --file links.txt
    python ingest.py --model small <链接>         # 默认跟着设备走
    python ingest.py --dir D:\\Notes <链接>        # 换库位置
    python ingest.py --force <链接>               # 无视去重，重转一次
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import server

# 默认库位置。⚠️ 别放 iCloudDrive：那边同步会把频繁写入的文件裂成冲突副本。
DEFAULT_ROOT = Path.home() / "Desktop" / "DouyinNotes"

_HASHTAG_RE = re.compile(r"#([^\s#@]+)")
_BAD_NAME_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def _split_tags(title: str) -> tuple[str, list[str]]:
    """把标题里的 #话题标签 抽出来，返回 (干净标题, 标签列表)。"""
    tags = [t.strip() for t in _HASHTAG_RE.findall(title or "") if t.strip()]
    clean = _HASHTAG_RE.sub(" ", title or "")
    # 标签常夹在标题中间，挖掉后会留下一串空格，这里压平。
    clean = re.sub(r"\s{2,}", " ", clean).strip(" -·、，,")
    return clean or (title or "").strip(), tags


def _resolve_video_id(url: str) -> tuple[str, str]:
    """
    把 v.douyin.com 短链跟到真实地址，顺手拿视频 ID，返回 (最终URL, video_id)。

    没有这一步的话，重复的短链要先跑完整套浏览器抓取（约 18 秒）才发现是
    已入库的，去重等于白做。这里只发一个轻量请求，失败就原样返回。
    """
    match = re.search(r"/video/(\d+)", url)
    if match:
        return url, match.group(1)
    try:
        import urllib.request

        req = urllib.request.Request(
            url,
            headers={"User-Agent": server._UA, "Accept-Language": "zh-CN,zh;q=0.9"},
        )
        with urllib.request.urlopen(req, context=server._SSL_CTX, timeout=15) as resp:
            final = resp.geturl()
    except Exception:
        return url, ""
    match = re.search(r"/video/(\d+)", final)
    return final, (match.group(1) if match else "")


def _media_duration(path: str) -> float:
    try:
        import av

        with av.open(path) as container:
            if container.duration:
                return round(container.duration / 1_000_000, 1)
    except Exception:
        pass
    return 0.0


def _fmt_duration(seconds: float) -> str:
    total = int(round(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _safe_name(text: str, limit: int = 40) -> str:
    cleaned = _BAD_NAME_RE.sub("_", (text or "").strip())
    cleaned = re.sub(r"_{2,}", "_", cleaned).strip("_ .")
    return cleaned[:limit]


def _yaml_escape(value: str) -> str:
    return '"' + (value or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


class Library:
    """库目录 + 去重索引。索引是 jsonl，坏了也只坏一行。"""

    def __init__(self, root: Path):
        self.root = root
        self.inbox = root / "inbox"
        self.unsorted = root / "_待分类"
        self.index_path = root / "index.jsonl"
        for directory in (self.inbox, self.unsorted):
            directory.mkdir(parents=True, exist_ok=True)
        self._seen = self._load_index()

    def _load_index(self) -> dict[str, dict]:
        seen: dict[str, dict] = {}
        if not self.index_path.exists():
            return seen
        with self.index_path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = row.get("video_id") or row.get("url")
                if key:
                    seen[key] = row
        return seen

    def known(self, video_id: str) -> dict | None:
        return self._seen.get(video_id)

    def record(self, row: dict) -> None:
        key = row.get("video_id") or row.get("url")
        if key:
            self._seen[key] = row
        with self.index_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def write_note(self, meta: dict, transcript: str) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        parts = [stamp, meta.get("platform", "video")]
        if meta.get("video_id"):
            parts.append(meta["video_id"])
        name_hint = _safe_name(meta.get("title", ""))
        if name_hint:
            parts.append(name_hint)
        path = self.inbox / ("_".join(parts) + ".md")

        tags = meta.get("tags") or []
        lines = [
            "---",
            f'title: {_yaml_escape(meta.get("title", ""))}',
            f'source: {_yaml_escape(meta.get("url", ""))}',
            f'platform: {meta.get("platform", "")}',
            f'video_id: {_yaml_escape(meta.get("video_id", ""))}',
            "tags: [" + ", ".join(_yaml_escape(t) for t in tags) + "]",
            'category: ""',
            "status: raw",
            f'duration: {meta.get("duration", 0)}',
            f'chars: {meta.get("chars", 0)}',
            f'model: {meta.get("model", "")}',
            f'device: {_yaml_escape(meta.get("device", ""))}',
            f'transcribed_at: {time.strftime("%Y-%m-%d %H:%M:%S")}',
            "---",
            "",
            "## 转写稿",
            "",
            transcript.strip(),
            "",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path


async def ingest_one(url_text: str, lib: Library, model: str, force: bool) -> str:
    """处理一条链接，返回给 stdout 的那一行摘要（绝不包含转写稿正文）。"""
    try:
        url = server._extract_url(url_text)
        platform = server._detect_platform(url)
    except Exception as exc:
        return f"[fail] 链接无法识别 | {type(exc).__name__}: {str(exc)[:80]}"

    url, video_id = _resolve_video_id(url)

    if video_id and not force:
        known = lib.known(video_id)
        if known:
            return f"[skip] {video_id} | 已入库，跳过 | {known.get('path', '')}"

    started = time.time()
    with tempfile.TemporaryDirectory(prefix="ingest_") as tmp:
        try:
            media_path, platform = await server._download_transcription_media(url, tmp)
        except Exception as exc:
            # server 那边的报错自带 [登录]/[验证码]/[媒体获取]/[下载] 阶段标记。
            first = str(exc).strip().splitlines()[0]
            return f"[fail] {video_id or url[-24:]} | {first[:110]}"

        meta = server.capture_meta(media_path)
        raw_title = meta.get("title", "")
        video_id = meta.get("video_id") or video_id
        duration = _media_duration(media_path)

        if video_id and not force:
            known = lib.known(video_id)
            if known:
                return f"[skip] {video_id} | 已入库，跳过 | {known.get('path', '')}"

        try:
            transcript = server._transcribe_sync(media_path, model)
        except Exception as exc:
            return f"[fail] {video_id} | 转录失败 {type(exc).__name__}: {str(exc)[:80]}"

    title, tags = _split_tags(raw_title)
    chars = len(transcript.replace("\n", "").replace(" ", ""))
    row_meta = {
        "title": title,
        "url": url,
        "platform": platform,
        "video_id": video_id,
        "tags": tags,
        "duration": duration,
        "chars": chars,
        "model": model,
        "device": server.device_label(),
    }
    path = lib.write_note(row_meta, transcript)
    lib.record({
        "video_id": video_id,
        "url": url,
        "title": title,
        "tags": tags,
        "path": str(path),
        "status": "raw",
        "chars": chars,
        "duration": duration,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })

    tag_hint = ("#" + " #".join(tags[:3])) if tags else "无标签"
    if not transcript.strip():
        return f"[warn] {video_id} | {title[:28]} | 无语音内容 | {path.name}"
    return (
        f"[ok] {video_id} | {title[:28]} | {_fmt_duration(duration)} | "
        f"{chars}字 | {tag_hint} | {time.time() - started:.0f}s | {path.name}"
    )


def read_urls(args) -> list[str]:
    urls = list(args.urls)
    if args.file:
        for line in Path(args.file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


async def main_async(args) -> int:
    urls = read_urls(args)
    if not urls:
        print("没有输入链接。用法：python ingest.py <链接> [更多链接...]")
        return 2

    lib = Library(Path(args.dir))
    model = args.model or server.recommended_model()
    print(f"库 {lib.root} | 模型 {model} | 设备 {server.device_label()} | 共 {len(urls)} 条")

    ok = skipped = failed = 0
    for url in urls:
        line = await ingest_one(url, lib, model, args.force)
        print(line, flush=True)
        if line.startswith("[ok]") or line.startswith("[warn]"):
            ok += 1
        elif line.startswith("[skip]"):
            skipped += 1
        else:
            failed += 1

    print(f"完成：入库 {ok}，跳过 {skipped}，失败 {failed}。待分类都在 {lib.inbox}")
    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="抖音/Bilibili 链接批量转录入库")
    parser.add_argument("urls", nargs="*", help="链接或整段分享文本")
    parser.add_argument("--file", help="每行一个链接的文本文件")
    parser.add_argument("--dir", default=str(DEFAULT_ROOT), help=f"库目录（默认 {DEFAULT_ROOT}）")
    parser.add_argument("--model", help="Whisper 模型；默认按设备自动选")
    parser.add_argument("--force", action="store_true", help="无视去重，重新转录")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
