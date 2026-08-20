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
        # 文件位置 = 状态：inbox/ 是还没写笔记的，notes/ 是写完的。
        # 这样在文件管理器里一眼看得出来，不用打开文件看 frontmatter。
        self.inbox = root / "inbox"
        self.notes = root / "notes"
        self.unsorted = root / "_待分类"
        self.index_path = root / "index.jsonl"
        for directory in (self.inbox, self.notes, self.unsorted):
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


_FIELD_RE_CACHE: dict[str, "re.Pattern"] = {}


def read_front_matter(path: Path) -> dict:
    """读 .md 头部的 frontmatter。只认我们自己写的那几个字段，不引 yaml 依赖。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    meta: dict = {}
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            meta[key.strip()] = [
                v.strip().strip('"') for v in inner.split(",") if v.strip()
            ] if inner else []
        else:
            meta[key.strip()] = value.strip('"')
    return meta


def scan_library(lib: Library) -> list[tuple[Path, dict]]:
    """扫遍库里所有 .md（备份目录除外），返回 (路径, frontmatter)。"""
    rows = []
    for path in sorted(lib.root.rglob("*.md")):
        if any(part.startswith("_测试") for part in path.parts):
            continue
        meta = read_front_matter(path)
        if meta.get("video_id") or meta.get("title"):
            rows.append((path, meta))
    return rows


def cmd_list(lib: Library) -> int:
    """一览：谁写过笔记、谁还没写。不打印任何正文。"""
    rows = scan_library(lib)
    if not rows:
        print(f"库是空的：{lib.root}")
        return 0
    raw = [r for r in rows if r[1].get("status") != "noted"]
    noted = [r for r in rows if r[1].get("status") == "noted"]

    def show(title: str, items) -> None:
        print(f"\n{title}（{len(items)} 条）")
        for path, meta in items:
            tags = meta.get("tags") or []
            tag_hint = ("#" + " #".join(tags[:3])) if tags else "无标签"
            category = meta.get("category") or "-"
            print(
                f"  {meta.get('video_id', ''):20s} {meta.get('chars', '0'):>5}字 "
                f"{category:10s} {tag_hint:34s} {meta.get('title', '')[:26]}"
            )

    print(f"库 {lib.root}")
    show("○ 未写笔记（inbox）", raw)
    show("● 已写笔记（notes）", noted)
    print(f"\n合计 {len(rows)} 条：未写 {len(raw)}，已写 {len(noted)}")
    return 0


def cmd_sync(lib: Library) -> int:
    """
    按 frontmatter 把文件挪到该在的位置，并重建索引。

    笔记写完（status 改成 noted）后跑一次，文件就从 inbox/ 移到
    notes/<category>/。分类还没定时 category 是空的，就直接放 notes/ 根下。
    """
    rows = scan_library(lib)
    moved = 0
    records = []
    for path, meta in rows:
        noted = meta.get("status") == "noted"
        category = (meta.get("category") or "").strip()
        if noted:
            target_dir = lib.notes / category if category else lib.notes
        else:
            target_dir = lib.inbox / category if category else lib.inbox
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / path.name
        if target != path:
            path.replace(target)
            moved += 1
            path = target
        records.append({
            "video_id": meta.get("video_id", ""),
            "url": meta.get("source", ""),
            "title": meta.get("title", ""),
            "tags": meta.get("tags") or [],
            "path": str(path),
            "status": meta.get("status", "raw"),
            "category": category,
            "chars": int(meta.get("chars") or 0),
            "duration": float(meta.get("duration") or 0),
            "at": meta.get("transcribed_at", ""),
        })
    with lib.index_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in records:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"归位 {moved} 个文件，索引重建 {len(records)} 条")
    return 0


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
    lib_only = Library(Path(args.dir))
    if args.list:
        return cmd_list(lib_only)
    if args.sync:
        return cmd_sync(lib_only)

    urls = read_urls(args)
    if not urls:
        print("没有输入链接。用法：python ingest.py <链接> [更多链接...]")
        return 2

    lib = lib_only
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
    parser.add_argument("--list", action="store_true", help="只看库里有什么、哪些还没写笔记")
    parser.add_argument("--sync", action="store_true", help="按 frontmatter 把文件归位并重建索引")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
