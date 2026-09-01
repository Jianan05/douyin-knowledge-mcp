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
import functools
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

if os.name == "nt":
    # Codex/重定向管道按 UTF-8 读取；显式设置可避免 Windows 默认 GBK 把中文回显弄乱码。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 如果仓库旁边有本地私有 runtime，命令行优先复用其浏览器和模型缓存。
# 否则 Playwright 会去
# %LOCALAPPDATA% 找 chromium（找不到，一条都转不了），
# HF 会把 whisper 模型重新下一份到 ~/.cache。外部已设值的照旧不覆盖。
_RUNTIME = Path(__file__).resolve().parent / "runtime"
_LOCAL_PLAYWRIGHT = _RUNTIME / "ms-playwright"
_LOCAL_HF = _RUNTIME / "huggingface"
if _LOCAL_PLAYWRIGHT.is_dir():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_LOCAL_PLAYWRIGHT))
if _LOCAL_HF.is_dir():
    os.environ.setdefault("HF_HOME", str(_LOCAL_HF))

import server

# 默认库位置。频繁写入的库建议使用本地非同步目录，避免产生冲突副本。
DEFAULT_ROOT = Path.home() / "Desktop" / "DouyinNotes"

_HASHTAG_RE = re.compile(r"#([^\s#@]+)")
_BAD_NAME_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')
_PROGRESS_WRITE_LOCK = threading.Lock()
_FAILURE_LOG_LOCK = threading.Lock()
_FAVORITES_PIPELINE_WORKERS = 4
_FAVORITES_ITEM_RETRY_DELAYS = (10, 30)


def _split_tags(title: str) -> tuple[str, list[str]]:
    """把标题里的 #话题标签 抽出来，返回 (干净标题, 标签列表)。"""
    tags = [t.strip() for t in _HASHTAG_RE.findall(title or "") if t.strip()]
    clean = _HASHTAG_RE.sub(" ", title or "")
    # 标签常夹在标题中间，挖掉后会留下一串空格，这里压平。
    clean = re.sub(r"\s{2,}", " ", clean).strip(" -·、，,")
    return clean or (title or "").strip(), tags


def _clean_url(url: str) -> str:
    """去掉分享链接上的追踪参数，留一个干净、能长期回溯的地址。"""
    return url.split("?")[0] if "douyin.com/video/" in url else url


def _one_line(text: str, limit: int = 70) -> str:
    """
    压成单行。

    抖音的「标题」其实是整段作品描述，经常带换行和项目符号。原样写进
    frontmatter 的 title: 会变成多行，**任何标准 YAML 解析器都会解析失败**
    （Obsidian、脚本都读不了）。所以 title 只留压平后的一行，完整描述
    另放 desc 字段和正文顶部。
    """
    flat = re.sub(r"\s+", " ", (text or "").replace("\n", " ")).strip()
    return flat[:limit]


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
    if total >= 3600:
        hours, rem = divmod(total, 3600)
        return f"{hours}:{rem // 60:02d}:{rem % 60:02d}"
    return f"{total // 60}:{total % 60:02d}"


def _safe_name(text: str, limit: int = 40) -> str:
    cleaned = _BAD_NAME_RE.sub("_", (text or "").strip())
    cleaned = re.sub(r"_{2,}", "_", cleaned).strip("_ .")
    return cleaned[:limit]


def _yaml_escape(value: str) -> str:
    return '"' + (value or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_progress_file(path: Path, text: str) -> None:
    """尽量原子更新进度；面板写失败绝不能中断真正的转录任务。"""
    with _PROGRESS_WRITE_LOCK:
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
        except OSError:
            return
        for _ in range(5):
            try:
                tmp.replace(path)
                return
            except OSError:
                # Windows 编辑器、杀毒或文件索引器可能短暂占住目标文件。
                time.sleep(0.1)
        try:
            # 原子替换持续失败时退化为直接覆盖；可见性次于转录不中断。
            path.write_text(text, encoding="utf-8")
        except OSError:
            pass
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


class Library:
    """库目录 + 去重索引。索引是 jsonl，坏了也只坏一行。"""

    def __init__(self, root: Path):
        self.root = root
        # 文件位置 = 状态：inbox/ 是还没写笔记的，notes/ 是写完的。
        # 这样在文件管理器里一眼看得出来，不用打开文件看 frontmatter。
        self.inbox = root / "inbox"
        self.notes = root / "notes"
        self.unsorted = root / "_待分类"
        self.replaced = root / "_已替换"
        self.source_packages = root / "_source_packages"
        self.index_path = root / "index.jsonl"
        self.pending_uncollect_path = root / "_待取消收藏.jsonl"
        self.progress_path = root / "_转录进度.md"
        self.failure_events_path = root / "_失败记录.jsonl"
        self.failure_report_path = root / "_失败记录.md"
        self.type_overrides_path = root / "_内容类型修正.jsonl"
        for directory in (self.inbox, self.notes, self.unsorted, self.source_packages):
            directory.mkdir(parents=True, exist_ok=True)
        self._write_failure_report()
        self._ensure_progress_file()
        self._seen = self._load_index()
        self._type_overrides = self._load_type_overrides()

    def _load_type_overrides(self) -> dict[str, dict]:
        overrides: dict[str, dict] = {}
        if not self.type_overrides_path.exists():
            return overrides
        with self.type_overrides_path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("aweme_id") and row.get("kind") in {"video", "image", "text"}:
                    overrides[row["aweme_id"]] = row
        return overrides

    def apply_type_override(self, item: dict) -> dict:
        override = self._type_overrides.get(item.get("aweme_id") or "")
        if not override:
            return item
        corrected = dict(item)
        corrected["kind"] = override["kind"]
        corrected["type_override_source"] = override.get("source") or "人工修正"
        if corrected["kind"] != "video":
            corrected["duration_ms"] = 0
        if corrected["kind"] == "text":
            corrected["is_image"] = False
        return corrected

    def _failure_events(self) -> list[dict]:
        events: list[dict] = []
        if not self.failure_events_path.exists():
            return events
        with self.failure_events_path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("aweme_id") and event.get("event"):
                    events.append(event)
        return events

    def _failure_states(self) -> dict[str, dict]:
        states: dict[str, dict] = {}
        for event in self._failure_events():
            aweme_id = event["aweme_id"]
            state = states.setdefault(
                aweme_id,
                {
                    "aweme_id": aweme_id,
                    "title": "",
                    "url": "",
                    "kind": "unknown",
                    "attempts": 0,
                    "first_failed_at": "",
                    "last_failed_at": "",
                    "last_reason": "",
                    "status": "pending",
                    "resolved_at": "",
                    "resolved_detail": "",
                    "recovered_note": "",
                    "workflow": "transcription",
                },
            )
            for field in ("title", "url", "kind", "workflow"):
                if event.get(field):
                    state[field] = event[field]
            if not event.get("workflow") and "视觉校准" in event.get("reason", ""):
                state["workflow"] = "visual_calibration"
            if event["event"] == "failed":
                state["attempts"] += 1
                state["first_failed_at"] = state["first_failed_at"] or event.get("at", "")
                state["last_failed_at"] = event.get("at", "")
                state["last_reason"] = event.get("reason", "")
                state["status"] = "pending"
                state["resolved_at"] = ""
                state["resolved_detail"] = ""
                if event.get("recovered_note"):
                    state["recovered_note"] = event["recovered_note"]
            elif event["event"] == "resolved":
                state["status"] = "resolved"
                state["resolved_at"] = event.get("at", "")
                state["resolved_detail"] = event.get("detail", "")
        return states

    def _render_failure_report(self) -> str:
        states = list(self._failure_states().values())
        pending = sorted(
            (row for row in states if row["status"] == "pending"),
            key=lambda row: row["last_failed_at"],
            reverse=True,
        )
        resolved = sorted(
            (row for row in states if row["status"] == "resolved"),
            key=lambda row: row["resolved_at"],
            reverse=True,
        )
        lines = [
            "# 抖音失败记录",
            "",
            "> 本文件永久保留失败与解决历史，不包含任何转录正文。重启批处理不会清空。",
            "",
            f"- 待重试：{len(pending)} 条",
            f"- 已解决：{len(resolved)} 条",
            f"- 最后更新：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "## 待重试",
            "",
        ]

        def append_state(row: dict) -> None:
            aweme_id = row["aweme_id"]
            title = _one_line(row["title"], 100) or f"作品 {aweme_id}"
            title = title.replace("[", "［").replace("]", "］")
            url = row["url"]
            if not url:
                route = "note" if row["kind"] in {"image", "text"} else "video"
                url = f"https://www.douyin.com/{route}/{aweme_id}"
            lines.append(f"- [{title}]({url}) (`{aweme_id}`)")
            workflow_label = {
                "visual_calibration": "视觉校准（本地关键帧/分镜处理）",
                "transcription": "批量入库",
            }.get(row["workflow"], row["workflow"])
            lines.append(f"  - 失败环节：{workflow_label}")
            lines.append(f"  - 最近原因：{_one_line(row['last_reason'], 240)}")
            lines.append(
                f"  - 失败次数：{row['attempts']}；首次：{row['first_failed_at'] or '时间未保存'}；"
                f"最近：{row['last_failed_at'] or '时间未保存'}"
            )
            lines.append("  - 收藏：已保留")
            if row["recovered_note"]:
                lines.append(f"  - 备注：{_one_line(row['recovered_note'], 180)}")
            if row["status"] == "resolved":
                lines.append(
                    f"  - ✅ 已解决：{row['resolved_at'] or '时间未保存'}"
                    + (
                        f"；{_one_line(row['resolved_detail'], 180)}"
                        if row["resolved_detail"]
                        else ""
                    )
                )

        if pending:
            for row in pending:
                append_state(row)
        else:
            lines.append("- 暂无待重试作品。")
        lines.extend(["", "## 已解决", ""])
        if resolved:
            for row in resolved:
                append_state(row)
        else:
            lines.append("- 暂无已解决记录。")
        return "\n".join(lines) + "\n"

    def _write_failure_report(self) -> None:
        _write_progress_file(self.failure_report_path, self._render_failure_report())

    def _append_failure_event(self, event: dict) -> None:
        with _FAILURE_LOG_LOCK:
            with self.failure_events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._write_failure_report()

    def record_failure(
        self, item: dict, reason: str, workflow: str = "transcription"
    ) -> None:
        aweme_id = item.get("aweme_id") or ""
        if not aweme_id:
            return
        self._append_failure_event(
            {
                "event": "failed",
                "aweme_id": aweme_id,
                "title": item.get("title") or item.get("desc") or "",
                "url": item.get("url") or "",
                "kind": item.get("kind") or "unknown",
                "workflow": workflow,
                "reason": _one_line(reason, 500),
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

    def resolve_failure(self, item: dict, detail: str = "") -> None:
        aweme_id = item.get("aweme_id") or ""
        state = self._failure_states().get(aweme_id)
        if not state or state["status"] != "pending":
            return
        self._append_failure_event(
            {
                "event": "resolved",
                "aweme_id": aweme_id,
                "title": item.get("title") or item.get("desc") or state["title"],
                "url": item.get("url") or state["url"],
                "kind": item.get("kind") or state["kind"],
                "workflow": state["workflow"],
                "detail": _one_line(detail, 300),
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

    def _ensure_progress_file(self) -> None:
        """首次使用库时建立可见的空闲状态文件；已有历史进度绝不覆盖。"""
        if self.progress_path.exists():
            return
        text = "\n".join([
            "# 抖音批量转录进度",
            "",
            "> 本文件由程序自动更新；只记录状态，不包含任何转录正文。",
            "",
            "- 状态：**未启动**",
            "- 当前作品：无",
            "- 当前阶段：等待启动批量任务",
            "- 待取消收藏队列：0 条",
            f"- 最后更新：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ])
        _write_progress_file(self.progress_path, text)

    def write_progress_status(self, status: str, stage: str, detail: str = "") -> None:
        """在尚未拿到收藏总数时也写出可见状态，例如清点收藏。"""
        stage_text = stage + (f"（{_one_line(detail, 120)}）" if detail else "")
        text = "\n".join([
            "# 抖音批量转录进度",
            "",
            "> 本文件由程序自动更新；只记录状态，不包含任何转录正文。",
            "",
            f"- 状态：**{status}**",
            "- 当前作品：无",
            f"- 当前阶段：{stage_text}",
            f"- 待取消收藏队列：{len(self.pending_uncollect())} 条",
            f"- 最后更新：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ])
        _write_progress_file(self.progress_path, text)

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
        row = self._seen.get(video_id)
        if not row:
            return None
        raw_path = str(row.get("path") or "").strip()
        if not raw_path:
            return None
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = self.root / path
        return row if path.is_file() else None

    def retire(self, video_id: str) -> str:
        """--force 重转时，把同一视频的旧文件挪进 _已替换/，不直接删。"""
        old = self._seen.get(video_id)
        if not old:
            return ""
        old_path = Path(old.get("path", ""))
        if not old_path.is_file():
            return ""
        self.replaced.mkdir(parents=True, exist_ok=True)
        target = self.replaced / old_path.name
        old_path.replace(target)
        return target.name

    def record(self, row: dict) -> None:
        key = row.get("video_id") or row.get("url")
        if key:
            self._seen[key] = row
        with self.index_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def pending_uncollect(self) -> list[dict]:
        rows: dict[str, dict] = {}
        if not self.pending_uncollect_path.exists():
            return []
        with self.pending_uncollect_path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("aweme_id"):
                    rows[row["aweme_id"]] = row
        return list(rows.values())

    def save_pending_uncollect(self, rows: list[dict]) -> None:
        """整文件原子更新待取消队列；这里只存 ID/URL，不存转写正文。"""
        unique = {r.get("aweme_id"): r for r in rows if r.get("aweme_id")}
        if not unique:
            self.pending_uncollect_path.unlink(missing_ok=True)
            return
        tmp = self.pending_uncollect_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for row in unique.values():
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        tmp.replace(self.pending_uncollect_path)

    def queue_uncollect(self, items: list[dict]) -> list[dict]:
        rows = {r["aweme_id"]: r for r in self.pending_uncollect()}
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        for item in items:
            aid = item.get("aweme_id") or ""
            if aid:
                rows[aid] = {
                    "aweme_id": aid,
                    "url": item.get("url") or "",
                    "queued_at": now,
                }
        result = list(rows.values())
        self.save_pending_uncollect(result)
        return result

    def write_note(self, meta: dict, transcript: str) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        parts = [stamp, meta.get("platform", "video")]
        if meta.get("video_id"):
            parts.append(meta["video_id"])
        name_hint = _safe_name(_one_line(meta.get("title", ""), 60))
        if name_hint:
            parts.append(name_hint)
        path = self.inbox / ("_".join(parts) + ".md")

        tags = meta.get("tags") or []
        full_desc = _one_line(meta.get("title", ""), 500)
        url = meta.get("url", "")
        lines = [
            "---",
            f'title: {_yaml_escape(_one_line(meta.get("title", "")))}',
            f'desc: {_yaml_escape(full_desc)}',
            f'source: {_yaml_escape(url)}',
            f'platform: {meta.get("platform", "")}',
            f'video_id: {_yaml_escape(meta.get("video_id", ""))}',
            "tags: [" + ", ".join(_yaml_escape(t) for t in tags) + "]",
            'category: ""',
            "status: raw",
            f'duration: {meta.get("duration", 0)}',
            f'chars: {meta.get("chars", 0)}',
            f'model: {meta.get("model", "")}',
            f'device: {_yaml_escape(meta.get("device", ""))}',
            f'source_package: {_yaml_escape(meta.get("source_package", ""))}',
            f'transcribed_at: {time.strftime("%Y-%m-%d %H:%M:%S")}',
            "---",
            "",
            # 正文顶部放一行可点击链接：转写稿只有声音，画面得回原视频看。
            f"🔗 [在抖音打开原视频]({url})",
            "",
            f"> {full_desc}" if full_desc else "",
            "",
            "## 转写稿",
            "",
            transcript.strip(),
            "",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def write_source_package(
        self,
        meta: dict,
        transcript: str,
        *,
        screen_ocr: str = "",
        screen_candidates: list[tuple[str, int]] | None = None,
    ) -> Path | None:
        """原子保存机器读取的来源包；普通旧式字符串不伪造时间戳。"""
        if not hasattr(transcript, "segments") or not hasattr(transcript, "asr"):
            return None
        video_id = str(meta.get("video_id") or "").strip()
        if not video_id:
            raise ValueError("无法写来源包：缺少 video_id")

        segments = []
        for index, row in enumerate(getattr(transcript, "segments", [])):
            item = dict(row)
            item["segment_id"] = f"{video_id}#{index:06d}"
            segments.append(item)
        cleaned_text = str(transcript)
        package = {
            "schema_version": 1,
            "package_type": "timestamped_clean_transcript",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": {
                "platform": meta.get("platform", ""),
                "video_id": video_id,
                "url": meta.get("url", ""),
                "title": meta.get("title", ""),
                "tags": meta.get("tags") or [],
                "duration": meta.get("duration", 0),
            },
            "asr": {
                **dict(getattr(transcript, "asr", {})),
                "device": meta.get("device", ""),
            },
            "segments": segments,
            "cleaned_text_sha256": hashlib.sha256(
                cleaned_text.encode("utf-8")
            ).hexdigest(),
            "cleaned_chars": len(cleaned_text.replace("\n", "").replace(" ", "")),
            "supplements": {
                "screen_ocr": screen_ocr,
                "screen_candidates": [
                    {"text": text, "frame_count": count}
                    for text, count in (screen_candidates or [])
                ],
            },
        }
        path = self.source_packages / f"{_safe_name(video_id, 100)}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(package, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
        return path


class TranscriptionProgress:
    """把批量转录状态原子写进库根目录的 Markdown，不保存转录正文。"""

    def __init__(
        self,
        lib: Library,
        *,
        mode: str,
        total: int,
        pending: int,
        eta_low_s: float,
        eta_high_s: float,
    ) -> None:
        self.lib = lib
        self.path = lib.progress_path
        self.mode = mode
        self.total = total
        self.pending = pending
        self.eta_low_s = eta_low_s
        self.eta_high_s = eta_high_s
        self.started = time.time()
        self.status = "运行中"
        self.current_index = 0
        self.current_id = ""
        self.current_title = ""
        self.stage_name = "准备"
        self.stage_detail = ""
        self.stage_percent: float | None = None
        self.ok = 0
        self.skipped = 0
        self.failed = 0
        self.failures: list[str] = []
        self.active: dict[str, dict] = {}
        self._state_lock = threading.RLock()
        self._last_write = 0.0
        self._write(force=True)

    @staticmethod
    def _clock(seconds: float) -> str:
        seconds = max(0, int(seconds))
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours} 小时 {minutes} 分"
        if minutes:
            return f"{minutes} 分 {secs} 秒"
        return f"{secs} 秒"

    def _remaining_eta(self) -> str:
        processed_work = self.ok + self.failed
        if self.pending <= 0 or processed_work >= self.pending:
            return "约 0 分钟"
        ratio = (self.pending - processed_work) / self.pending
        return f"{_fmt_eta(self.eta_low_s * ratio)}～{_fmt_eta(self.eta_high_s * ratio)}"

    def _render(self) -> str:
        current = "无"
        if self.current_id or self.current_title:
            current = f"{self.current_id}　{_one_line(self.current_title, 80)}".strip()
        stage = self.stage_name
        if self.stage_percent is not None:
            stage += f" {self.stage_percent:.0%}"
        if self.stage_detail:
            stage += f"（{self.stage_detail}）"
        lines = [
            "# 抖音批量转录进度",
            "",
            "> 本文件由程序自动更新；只记录状态，不包含任何转录正文。",
            "",
            f"- 状态：**{self.status}**",
            f"- 模式：{self.mode}",
            f"- 收藏清点：{self.total} 条",
            f"- 本轮待处理：{self.pending} 条",
            f"- 已完成：{self.ok} 条",
            f"- 已跳过：{self.skipped} 条",
            f"- 失败：{self.failed} 条",
            f"- 永久失败记录：[打开 _失败记录.md]({self.lib.failure_report_path.name})",
            f"- 当前进度：{self.current_index}/{self.total}",
            f"- 并行处理中：{len(self.active)} 条",
            f"- 当前作品：{current}",
            f"- 当前阶段：{stage}",
            f"- 已运行：{self._clock(time.time() - self.started)}",
            f"- 预计剩余：{self._remaining_eta()}",
            f"- 待取消收藏队列：{len(self.lib.pending_uncollect())} 条",
            f"- 最后更新：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        if self.active:
            lines.extend(["", "## 并行任务", ""])
            for active in sorted(self.active.values(), key=lambda row: row["index"]):
                active_stage = active["stage"]
                if active["percent"] is not None:
                    active_stage += f" {active['percent']:.0%}"
                if active["detail"]:
                    active_stage += f"（{active['detail']}）"
                title = _one_line(active["title"], 55)
                lines.append(
                    f"- {active['index']}/{self.total}　{active['aweme_id']}　"
                    f"{title} — {active_stage}"
                )
        if self.failures:
            lines.extend(["", "## 本轮失败清单", ""])
            lines.extend(f"- {failure}" for failure in self.failures)
        return "\n".join(lines) + "\n"

    def _write(self, *, force: bool = False) -> None:
        with self._state_lock:
            now = time.time()
            if not force and now - self._last_write < 0.8:
                return
            rendered = self._render()
            self._last_write = now
        _write_progress_file(self.path, rendered)

    def begin_item(self, index: int, item: dict, stage: str) -> "ItemProgress":
        aweme_id = item.get("aweme_id") or ""
        title = item.get("desc") or ""
        key = f"{index}:{aweme_id}"
        with self._state_lock:
            self.current_index = max(self.current_index, index)
            self.current_id = aweme_id
            self.current_title = title
            self.stage_name = stage
            self.stage_detail = ""
            self.stage_percent = None
            self.active[key] = {
                "index": index,
                "aweme_id": aweme_id,
                "title": title,
                "url": item.get("url") or "",
                "kind": item.get("kind") or (
                    "image" if item.get("is_image") else "video"
                ),
                "stage": stage,
                "detail": "",
                "percent": None,
            }
        self._write(force=True)
        return ItemProgress(self, key)

    def stage(
        self,
        name: str,
        *,
        percent: float | None = None,
        detail: str = "",
        force: bool = False,
    ) -> None:
        with self._state_lock:
            self.stage_name = name
            self.stage_percent = (
                None if percent is None else max(0.0, min(1.0, percent))
            )
            self.stage_detail = _one_line(detail, 80)
        self._write(force=force)

    def update_item(
        self,
        key: str,
        name: str,
        *,
        percent: float | None = None,
        detail: str = "",
        force: bool = False,
    ) -> None:
        normalized_percent = (
            None if percent is None else max(0.0, min(1.0, percent))
        )
        normalized_detail = _one_line(detail, 80)
        with self._state_lock:
            active = self.active.get(key)
            if active is None:
                return
            active["stage"] = name
            active["percent"] = normalized_percent
            active["detail"] = normalized_detail
            self.current_id = active["aweme_id"]
            self.current_title = active["title"]
            self.stage_name = name
            self.stage_percent = normalized_percent
            self.stage_detail = normalized_detail
        self._write(force=force)

    def record(self, line: str, key: str | None = None) -> None:
        with self._state_lock:
            record_item = self.active.get(key) if key is not None else None
            if record_item is None and key is None and len(self.active) == 1:
                record_item = next(iter(self.active.values()))
            if line.startswith(("[ok]", "[warn]")):
                self.ok += 1
                if record_item:
                    self.lib.resolve_failure(record_item, line.split("|", 1)[-1].strip())
            elif line.startswith("[skip]"):
                self.skipped += 1
                if record_item:
                    self.lib.resolve_failure(record_item, "已确认此前成功入库")
            else:
                self.failed += 1
                if record_item:
                    aweme_id = record_item["aweme_id"]
                    title = _one_line(record_item["title"], 80) or f"作品 {aweme_id}"
                    title = title.replace("[", "［").replace("]", "］")
                    url = record_item["url"]
                    if not url and aweme_id:
                        route = (
                            "note"
                            if record_item["kind"] in {"image", "text"}
                            else "video"
                        )
                        url = f"https://www.douyin.com/{route}/{aweme_id}"
                    reason = line.split("|", 1)[-1].strip()
                    self.lib.record_failure(record_item, reason)
                    link = f"[在抖音打开原作品]({url})" if url else "原作品链接缺失"
                    self.failures.append(
                        f"{title} (`{aweme_id}`) — {reason} — {link} — 收藏：已保留"
                    )
                else:
                    self.failures.append(_one_line(line, 180))
            if key is not None:
                self.active.pop(key, None)
            if self.active:
                latest = max(self.active.values(), key=lambda row: row["index"])
                self.current_id = latest["aweme_id"]
                self.current_title = latest["title"]
                self.stage_name = latest["stage"]
                self.stage_detail = latest["detail"]
                self.stage_percent = latest["percent"]
            else:
                self.stage_name = (
                    "本条完成" if not line.startswith("[fail]") else "本条失败"
                )
                self.stage_detail = ""
                self.stage_percent = 1.0
        self._write(force=True)

    def finish(self, status: str) -> None:
        with self._state_lock:
            self.status = status
            self.active.clear()
            self.current_id = ""
            self.current_title = ""
            self.stage_name = "批量任务结束"
            self.stage_detail = ""
            self.stage_percent = 1.0
        self._write(force=True)


class ItemProgress:
    """一条并行作品绑定的进度句柄，避免不同任务互相串台。"""

    def __init__(self, report: TranscriptionProgress, key: str) -> None:
        self.report = report
        self.key = key

    def stage(
        self,
        name: str,
        *,
        percent: float | None = None,
        detail: str = "",
        force: bool = False,
    ) -> None:
        self.report.update_item(
            self.key,
            name,
            percent=percent,
            detail=detail,
            force=force,
        )


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
        # 下划线开头的目录是归档区（_已替换 / 备份），只有 _待分类 参与流转。
        if any(
            part.startswith("_") and part != "_待分类" for part in path.parts
        ):
            continue
        meta = read_front_matter(path)
        if meta.get("video_id") or meta.get("title"):
            rows.append((path, meta))
    return rows


_RULES_DIR = Path(__file__).resolve().parent
_PUBLIC_RULES_PATH = _RULES_DIR / "categories.toml"
_LOCAL_RULES_PATH = _RULES_DIR / "categories.local.toml"
_RULES_OVERRIDE = os.environ.get("DOUYIN_CATEGORIES_FILE", "").strip()


def _resolve_rules_path(rules_dir: Path = _RULES_DIR, override: str | None = None) -> Path:
    """Resolve rules with explicit override > local file > public defaults."""
    selected_override = _RULES_OVERRIDE if override is None else override.strip()
    if selected_override:
        return Path(selected_override).expanduser()
    local_path = rules_dir / "categories.local.toml"
    return local_path if local_path.is_file() else rules_dir / "categories.toml"


RULES_PATH = _resolve_rules_path()


def load_rules(path: Path = RULES_PATH) -> list[dict]:
    """读分类规则表。顺序即优先级，第一条命中就定。"""
    if not path.is_file():
        return []
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib

    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return data.get("rule", [])


def match_category(meta: dict, rules: list[dict]) -> str:
    title = (meta.get("title") or "").lower()
    tags = {t.lower() for t in (meta.get("tags") or [])}
    for rule in rules:
        if tags & {t.lower() for t in rule.get("tags", [])}:
            return rule["name"]
        if any(k.lower() in title for k in rule.get("keywords", [])):
            return rule["name"]
    return ""


def set_front_matter_field(path: Path, key: str, value: str) -> None:
    """只改 frontmatter 里的一个字段，正文一个字节不动。"""
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(rf"^{re.escape(key)}: .*$", re.M)
    replacement = f'{key}: "{value}"'
    head, sep, body = text.partition("\n---")
    if not sep:
        return
    if pattern.search(head):
        head = pattern.sub(replacement, head, count=1)
    else:
        head = head.rstrip() + "\n" + replacement
    path.write_text(head + sep + body, encoding="utf-8")


def cmd_classify(lib: Library, apply: bool, recategorize: bool = False, brief: bool = False) -> int:
    """
    按 categories.toml 给还没分类的条目打上 category。

    默认只预览（--classify），确认没问题再加 --apply 落盘。
    分类只看标题和标签，**从不读转写稿正文**。

    recategorize=True 时无视已有的 category 重算。改了规则表想让老条目
    跟着走，必须加这个——默认是保留已有分类的，不然每次跑都会推翻人工调整。
    brief=True 只打印每类的条数，不逐条列。库大了以后逐条列会刷屏，
    而且如果是 AI 在跑，那一屏就是几万 token。
    """
    rules = load_rules()
    if not rules:
        print(f"没找到规则表：{RULES_PATH}")
        return 2

    buckets: dict[str, list] = {}
    for path, meta in scan_library(lib):
        existing = (meta.get("category") or "").strip()
        category = match_category(meta, rules) if recategorize else (
            existing or match_category(meta, rules)
        )
        buckets.setdefault(category or "_待分类", []).append((path, meta, category))

    total = sum(len(v) for v in buckets.values())
    for name in sorted(buckets, key=lambda k: (k == "_待分类", -len(buckets[k]))):
        items = buckets[name]
        if brief:
            print(f"{len(items):5d}  {len(items) * 100 // max(total, 1):2d}%  {name}")
            continue
        print(f"\n{name}（{len(items)} 条）")
        for path, meta, _ in items:
            print(f"  {int(meta.get('chars') or 0):6d}字  {meta.get('title', '')[:34]}")

    if not apply:
        print("\n以上是预览。确认无误后加 --apply 落盘，再跑 --sync 归位。")
        return 0

    changed = 0
    for name, items in buckets.items():
        if name == "_待分类":
            # 重算时没命中的，要把旧 category 清掉，否则文件会一直卡在
            # 老目录里（改规则表后最容易漏的一步）。
            if recategorize:
                for path, meta, _ in items:
                    if (meta.get("category") or "").strip():
                        set_front_matter_field(path, "category", "")
                        changed += 1
            continue
        for path, meta, category in items:
            if category and ((meta.get("category") or "").strip() != category):
                set_front_matter_field(path, "category", category)
                changed += 1
    print(f"\n已写入 {changed} 条的 category 字段。接着跑 --sync 把文件归位。")
    return 0


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
            "source_package": meta.get("source_package", ""),
            "at": meta.get("transcribed_at", ""),
        })
    with lib.index_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in records:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"归位 {moved} 个文件，索引重建 {len(records)} 条")
    return 0


async def ingest_one(url_text: str, lib: Library, model: str, force: bool,
                     no_screen: bool = False,
                     progress: TranscriptionProgress | ItemProgress | None = None,
                     download_lock: asyncio.Lock | None = None,
                     title_hint: str = "",
                     collected_video: dict | None = None) -> str:
    """处理一条链接，返回给 stdout 的那一行摘要（绝不包含转写稿正文）。"""
    try:
        url = server._extract_url(url_text)
        platform = server._detect_platform(url)
    except Exception as exc:
        return f"[fail] 链接无法识别 | {type(exc).__name__}: {str(exc)[:80]}"

    url, video_id = _resolve_video_id(url)
    url = _clean_url(url)

    if video_id and not force:
        known = lib.known(video_id)
        if known:
            return f"[skip] {video_id} | 已入库，跳过 | {known.get('path', '')}"

    started = time.time()
    with tempfile.TemporaryDirectory(prefix="ingest_") as tmp:
        try:
            if progress:
                progress.stage("下载音频", force=True)

            def on_download(done: int, total: int | None) -> None:
                if not progress:
                    return
                ratio = done / total if total else None
                detail = f"{done / 1024 / 1024:.1f} MB"
                progress.stage("下载音频", percent=ratio, detail=detail)

            if collected_video:
                media_path = await server._download_douyin_video_object(
                    collected_video, tmp, need="audio", on_progress=on_download
                )
                platform = "douyin"
            elif download_lock is None:
                media_path, platform = await server._download_transcription_media(
                    url, tmp, on_progress=on_download
                )
            else:
                async with download_lock:
                    media_path, platform = await server._download_transcription_media(
                        url, tmp, on_progress=on_download
                    )
        except Exception as exc:
            # server 那边的报错自带 [登录]/[验证码]/[媒体获取]/[下载] 阶段标记。
            first = str(exc).strip().splitlines()[0]
            return f"[fail] {video_id or url[-24:]} | {first[:110]}"

        meta = server.capture_meta(media_path)
        # 收藏接口的 desc 与 aweme_id 是同一条响应里的数据，优先级高于页面捕获；
        # 页面会预载推荐作品，历史上曾把推荐视频标题覆盖到收藏作品上。
        raw_title = title_hint or meta.get("title", "")
        captured_id = meta.get("video_id") or ""
        if captured_id and video_id and captured_id != video_id:
            return f"[fail] {video_id} | 媒体作品ID错配（捕获到 {captured_id}），已拒绝保存"
        video_id = video_id or captured_id
        duration = _media_duration(media_path)

        if video_id and not force:
            known = lib.known(video_id)
            if known:
                return f"[skip] {video_id} | 已入库，跳过 | {known.get('path', '')}"

        try:
            if progress:
                progress.stage("Whisper 语音识别", force=True)

            def on_segment(end: float, total: float, _text: str) -> None:
                if progress:
                    progress.stage(
                        "Whisper 语音识别",
                        percent=end / total if total else None,
                        detail=f"{_fmt_duration(end)}/{_fmt_duration(total)}" if total else "",
                    )

            loop = asyncio.get_running_loop()
            transcript = await loop.run_in_executor(
                server._TRANSCRIBE_EXECUTOR,
                functools.partial(
                    server._transcribe_segments_sync,
                    media_path,
                    model,
                    on_segment=on_segment,
                ),
            )
            timestamped_transcript = transcript
        except Exception as exc:
            return f"[fail] {video_id} | 转录失败 {type(exc).__name__}: {str(exc)[:80]}"

    title, tags = _split_tags(raw_title)

    # 声音里没内容 → 去画面里找。多下一次视频流，几十秒，只对少数视频触发。
    screen_note = ""
    screen = ""
    cands: list[tuple[str, int]] = []
    ocr_failed = False
    if not no_screen and platform == "douyin" and _looks_empty(transcript, duration):
        try:
            if progress:
                progress.stage("画面 OCR", force=True)
            screen, cands = await read_screen_text(
                url, transcript, download_lock=download_lock
            )
        except Exception as exc:
            screen, cands = "", []
            ocr_failed = True
            screen_note = f"（画面识别失败：{type(exc).__name__}）"
        if screen:
            screen_note = "## 画面文字" + chr(10) * 2 + screen
            if cands:
                hint = "、".join(f"{w}({n}帧)" for w, n in cands[:5])
                screen_note += chr(10) * 2 + f"> 画面里的英文候选：{hint}"
    if screen_note:
        transcript = (transcript.strip() + chr(10) * 2 + screen_note).strip()

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
    if progress:
        progress.stage("保存转录稿", force=True)
    source_package = lib.write_source_package(
        row_meta,
        timestamped_transcript,
        screen_ocr=screen,
        screen_candidates=cands,
    )
    if source_package:
        row_meta["source_package"] = source_package.relative_to(lib.root).as_posix()
    retired = lib.retire(video_id) if force else ""
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
        "source_package": row_meta.get("source_package", ""),
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })

    tag_hint = ("#" + " #".join(tags[:3])) if tags else "无标签"
    if retired:
        tag_hint += " (旧版已挪到 _已替换)"
    if ocr_failed:
        return f"[warn] {video_id} | {title[:28]} | 画面 OCR 失败，已保留收藏 | {path.name}"
    if not timestamped_transcript.strip() and not screen.strip():
        return f"[warn] {video_id} | {title[:28]} | 无语音且无有效 OCR，已保留收藏 | {path.name}"
    return (
        f"[ok] {video_id} | {title[:28]} | {_fmt_duration(duration)} | "
        f"{chars}字 | {tag_hint} | {time.time() - started:.0f}s | {path.name}"
    )


def _looks_empty(transcript: str, duration: float) -> bool:
    """转写稿相对时长少得离谱 = 这条视频的内容不在声音里，在画面上。

    判据是全库 QC 扫出来的：每秒不到 1.5 字，或绝对字数 < 20。
    典型场景：一张照片发成视频、纯演示无人声、纯 BGM 配字幕。
    """
    chars = len(transcript.replace("\n", "").replace(" ", ""))
    return chars < 20 or (duration > 20 and chars < duration * 1.5)


def _has_effective_image_ocr(text: str) -> bool:
    """Ignore generated headings/failure placeholders when deciding whether OCR found content."""
    content = re.sub(r"(?m)^### 图 \d+\s*$", "", text or "")
    content = re.sub(r"（(?:这张图没识别出文字|识别失败：[^）]+)）", "", content)
    return bool(content.strip())


async def read_screen_text(
    url: str,
    transcript: str,
    download_lock: asyncio.Lock | None = None,
) -> tuple[str, list]:
    """抽帧 OCR 兜底。返回 (画面文字, 英文候选名)。"""
    import frames

    with tempfile.TemporaryDirectory(prefix="frames_") as tmp:
        # ⚠️ 必须 need="video"：转写那条路下的是纯音频，一帧也抽不出来
        if download_lock is None:
            path = await server._download_douyin_media(url, tmp, need="video")
        else:
            async with download_lock:
                path = await server._download_douyin_media(url, tmp, need="video")
        screen, words = await asyncio.to_thread(frames.read_screen, path)
        return screen, frames.candidates(words, transcript)


async def ingest_image_post(
    item: dict,
    lib: Library,
    force: bool,
    progress: TranscriptionProgress | ItemProgress | None = None,
    ocr_semaphore: asyncio.Semaphore | None = None,
) -> str:
    """图文帖：图片 OCR 成文字，走跟视频稿同一套 frontmatter 和目录。"""
    import image_note

    video_id = item.get("aweme_id") or ""
    url = item.get("url") or ""
    if video_id and not force:
        known = lib.known(video_id)
        if known:
            return f"[skip] {video_id} | 已入库，跳过 | {known.get('path', '')}"

    started = time.time()
    images = item.get("images") or []
    if not images:
        return f"[fail] {video_id} | 图文没拿到图片地址"

    try:
        if progress:
            progress.stage("图文 OCR", detail=f"共 {len(images)} 张", force=True)
        if ocr_semaphore is None:
            transcript, done = await asyncio.to_thread(image_note.ocr_images, images)
        else:
            async with ocr_semaphore:
                transcript, done = await asyncio.to_thread(image_note.ocr_images, images)
    except Exception as exc:
        return f"[fail] {video_id} | OCR 失败 {type(exc).__name__}: {str(exc)[:80]}"

    title, tags = _split_tags(item.get("desc") or "")
    chars = len(transcript.replace(chr(10), "").replace(" ", ""))
    row_meta = {
        "title": title,
        "url": url,
        "platform": "douyin-图文",
        "video_id": video_id,
        "tags": tags,
        "duration": 0,
        "chars": chars,
        "model": f"rapidocr({done}/{len(images)}张)",
        "device": "CPU (OCR)",
    }
    if progress:
        progress.stage("保存图文文字", force=True)
    retired = lib.retire(video_id) if force else ""
    path = lib.write_note(row_meta, transcript)
    lib.record({
        "video_id": video_id,
        "url": url,
        "title": title,
        "tags": tags,
        "path": str(path),
        "status": "raw",
        "chars": chars,
        "duration": 0,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    tag_hint = ("#" + " #".join(tags[:3])) if tags else "无标签"
    if retired:
        tag_hint += " (旧版已挪到 _已替换)"
    if done < len(images):
        return (
            f"[warn] {video_id} | 图文 {title[:24]} | OCR 仅完成 {done}/{len(images)} 张，"
            f"已保留收藏 | {path.name}"
        )
    if not _has_effective_image_ocr(transcript):
        return (
            f"[warn] {video_id} | 图文 {title[:24]} | OCR 未提取到有效文字，"
            f"已保留收藏 | {path.name}"
        )
    return (
        f"[ok] {video_id} | 图文 {title[:24]} | {done}/{len(images)}张 | "
        f"{chars}字 | {tag_hint} | {time.time() - started:.0f}s | {path.name}"
    )


async def ingest_text_post(
    item: dict,
    lib: Library,
    force: bool,
    progress: TranscriptionProgress | ItemProgress | None = None,
) -> str:
    """抖音纯文字帖：正文直接入库，不跑 Whisper，也不做 OCR。"""
    aweme_id = item.get("aweme_id") or ""
    url = item.get("url") or ""
    if aweme_id and not force:
        known = lib.known(aweme_id)
        if known:
            return f"[skip] {aweme_id} | 已入库，跳过 | {known.get('path', '')}"

    body = (item.get("desc") or "").strip()
    if not body:
        return f"[fail] {aweme_id} | 纯文字帖没有拿到正文"
    if progress:
        progress.stage("保存原生文字", force=True)
    title, tags = _split_tags(body)
    title = title or _one_line(body, 60)
    chars = len(body.replace(chr(10), "").replace(" ", ""))
    meta = {
        "title": title,
        "url": url,
        "platform": "douyin-文字",
        "video_id": aweme_id,
        "tags": tags,
        "duration": 0,
        "chars": chars,
        "model": "原生文字",
        "device": "无需转录",
    }
    retired = lib.retire(aweme_id) if force else ""
    path = lib.write_note(meta, body)
    lib.record({
        "video_id": aweme_id,
        "url": url,
        "title": title,
        "tags": tags,
        "path": str(path),
        "status": "raw",
        "platform": "douyin-文字",
        "chars": chars,
        "duration": 0,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    moved = " | 旧版已挪到 _已替换" if retired else ""
    return f"[ok] {aweme_id} | 文字 {title[:28]} | {chars}字{moved} | {path.name}"


def _favorite_inventory(items: list[dict], lib: Library) -> dict:
    counts = {"video": 0, "image": 0, "text": 0, "unknown": 0}
    pending = []
    known = 0
    duration = 0.0
    image_count = 0
    for item in items:
        kind = item.get("kind") or ("image" if item.get("is_image") else "video")
        counts[kind if kind in counts else "unknown"] += 1
        if lib.known(item.get("aweme_id") or ""):
            known += 1
            continue
        pending.append(item)
        duration += float(item.get("duration_ms") or 0) / 1000
        image_count += len(item.get("images") or [])

    # 由历史本地运行估出的保守区间：视频约实时的 0.14~0.20 倍；
    # 图文按每帖 20~60 秒，再按多图略加开销；纯文字只需数秒。
    non_video_images = sum(1 for i in pending if i.get("kind") == "image")
    text_posts = sum(1 for i in pending if i.get("kind") == "text")
    eta_low = duration * 0.14 + non_video_images * 20 + image_count * 2 + text_posts * 2
    eta_high = duration * 0.20 + non_video_images * 60 + image_count * 6 + text_posts * 8
    return {
        "counts": counts,
        "known": known,
        "pending": pending,
        "duration_s": duration,
        "eta_low_s": eta_low,
        "eta_high_s": eta_high,
        "uncollect_s": len(items) * 9.5,
    }


def _fmt_eta(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"约 {minutes} 分钟"
    return f"约 {minutes / 60:.1f} 小时"


def _print_favorite_inventory(items: list[dict], lib: Library) -> dict:
    inv = _favorite_inventory(items, lib)
    c = inv["counts"]
    print(
        f"收藏清点：共 {len(items)} 条（视频 {c['video']}，图文 {c['image']}，"
        f"纯文字 {c['text']}，未知 {c['unknown']}）；已入库 {inv['known']}，"
        f"待处理 {len(inv['pending'])}"
    )
    print(
        f"待处理视频总时长 {_fmt_duration(inv['duration_s'])}；"
        f"预计入库 {_fmt_eta(inv['eta_low_s'])}～{_fmt_eta(inv['eta_high_s'])}；"
        f"取消全部收藏 {_fmt_eta(inv['uncollect_s'])}"
    )
    return inv


async def _ingest_collected_item(
    item: dict,
    lib: Library,
    model: str,
    force: bool,
    no_screen: bool,
    progress: TranscriptionProgress | ItemProgress | None = None,
    download_lock: asyncio.Lock | None = None,
    ocr_semaphore: asyncio.Semaphore | None = None,
) -> str:
    kind = item.get("kind") or ("image" if item.get("is_image") else "video")
    if kind == "image":
        return await ingest_image_post(item, lib, force, progress, ocr_semaphore)
    if kind == "text":
        return await ingest_text_post(item, lib, force, progress)
    if kind == "video":
        return await ingest_one(
            item["url"], lib, model, force, no_screen, progress, download_lock,
            item.get("desc") or "", item.get("_video") or None,
        )
    return f"[fail] {item.get('aweme_id', '')} | 未识别的作品类型，已保留收藏"


async def _run_pending_uncollect(lib: Library, force_now: bool = False) -> int:
    """处理持久化取消队列；先重新清点，防止把已取消的作品误点成重新收藏。"""
    import douyin_collects as dc

    queued = lib.pending_uncollect()
    if not queued:
        print("没有待取消收藏的作品。")
        return 0

    try:
        info, current = await dc.fetch_favorites(allow_empty=True)
    except Exception as exc:
        print(f"[fail] 无法在取消前重新清点收藏：{exc}")
        return 1
    if not info.get("complete"):
        print("[fail] 取消前重新清点不完整；已保留队列且未取消任何收藏。")
        return 1
    current_ids = {i["aweme_id"] for i in current}
    queued = [r for r in queued if r["aweme_id"] in current_ids]
    lib.save_pending_uncollect(queued)
    if not queued:
        print("队列中的作品均已不在收藏中，队列已清空。")
        return 0

    eta = len(queued) * 9.5
    if eta > 30 * 60 and not force_now:
        print(
            f"待取消 {len(queued)} 条，预计 {_fmt_eta(eta)}，超过 30 分钟；"
            f"已延后。确认现在执行可加 --force-uncollect。"
        )
        return 0

    print(f"开始取消 {len(queued)} 条收藏，预计 {_fmt_eta(eta)}…", flush=True)
    removed, missed = await dc.uncollect_items(queued)
    missed_set = set(missed)
    lib.save_pending_uncollect([r for r in queued if r["aweme_id"] in missed_set])
    print(
        f"确认取消 {len(removed)} 条，仍在收藏 {len(missed)} 条。"
        + (f"待重试清单：{lib.pending_uncollect_path}" if missed else "待取消队列已清空。")
    )
    return 0 if not missed else 1


async def cmd_favorites(args, lib: Library, model: str) -> int:
    """清点或处理抖音“收藏”总列表，不要求收藏夹名。"""
    import douyin_collects as dc

    if not args.dry_run:
        lib.write_progress_status("运行中", "清点全部收藏")
    try:
        # 只保存模式允许先处理“稳定可见”的部分：即使抖音错误地一直返回
        # has_more=1，也不会漏删收藏；后续重跑按 aweme_id 去重并补齐。
        # 会取消收藏的模式仍必须证明已经滚到底，安全门不放宽。
        info, items = await dc.fetch_favorites(
            require_exhausted=not args.keep_collected
        )
    except Exception as exc:
        if not args.dry_run:
            lib.write_progress_status("启动失败", "清点全部收藏失败", str(exc))
        print(f"[fail] 全部收藏：{exc}")
        return 1

    items = [lib.apply_type_override(item) for item in items]

    inventory_complete = bool(info.get("complete"))
    invalid_item_count = int(info.get("invalid_item_count") or 0)
    if invalid_item_count:
        print(
            f"[info] 抖音接口单次响应最多报告 {invalid_item_count} 条无效收藏记录；"
            "该字段不一定是累计总数。这些记录可能对应作者隐藏、删除或当前账号"
            "不可见的作品，未计入可处理清单。",
            flush=True,
        )
    if not inventory_complete:
        print(
            f"[warn] 抖音仍返回 has_more=1；先处理当前稳定可见的 {len(items)} 条。"
            "本轮强制只保存、不取消收藏，后续重跑会去重补齐。",
            flush=True,
        )

    max_items = int(getattr(args, "max_items", 0) or 0)
    if max_items > 0:
        candidates = items if args.force else [
            item for item in items if not lib.known(item.get("aweme_id") or "")
        ]
        items = candidates[:max_items]
        print(
            f"[test] 本轮仅处理 {len(items)} 条尚未入库作品；"
            "完整续跑时去掉 --max-items。",
            flush=True,
        )

    inv = _print_favorite_inventory(items, lib)
    if args.dry_run:
        if not args.brief:
            for item in items:
                kind = {"video": "视频", "image": "图文", "text": "文字"}.get(
                    item.get("kind"), "未知"
                )
                mark = "已入库" if lib.known(item["aweme_id"]) else "待处理"
                print(f"  [{mark}] {item['aweme_id']} {kind:>4}  {_one_line(item['desc'], 46)}")
        return 0

    pending_count = len(items) if args.force else len(inv["pending"])
    progress = TranscriptionProgress(
        lib,
        mode=(
            "当前稳定可见收藏（清点未到底；只保存、不取消收藏）"
            if not inventory_complete
            else "全部收藏（只保存，不取消收藏）"
        ) if args.keep_collected else "全部收藏",
        total=len(items),
        pending=pending_count,
        eta_low_s=inv["eta_low_s"],
        eta_high_s=inv["eta_high_s"],
    )
    print(f"进度文件：{progress.path}", flush=True)

    ok = skipped = failed = 0
    confirmed: list[dict] = []
    try:
        queue: asyncio.Queue[tuple[int, dict] | None] = asyncio.Queue()
        download_lock = asyncio.Lock()
        ocr_semaphore = asyncio.Semaphore(2)
        for indexed in enumerate(items, 1):
            queue.put_nowait(indexed)
        for _ in range(_FAVORITES_PIPELINE_WORKERS):
            queue.put_nowait(None)

        async def worker() -> None:
            nonlocal ok, skipped, failed
            while True:
                row = await queue.get()
                if row is None:
                    queue.task_done()
                    return
                index, item = row
                kind = item.get("kind") or (
                    "image" if item.get("is_image") else "video"
                )
                first_stage = {
                    "video": "检查去重",
                    "image": "检查图文",
                    "text": "检查原生文字",
                }.get(kind, "检查作品类型")
                item_progress = progress.begin_item(index, item, first_stage)
                line = ""
                for attempt in range(1, len(_FAVORITES_ITEM_RETRY_DELAYS) + 2):
                    try:
                        line = await _ingest_collected_item(
                            item,
                            lib,
                            model,
                            args.force,
                            args.no_screen,
                            item_progress,
                            download_lock,
                            ocr_semaphore,
                        )
                    except Exception as exc:
                        aid = item.get("aweme_id") or ""
                        line = (
                            f"[fail] {aid} | 流水线异常 {type(exc).__name__}: "
                            f"{str(exc)[:100]}"
                        )
                    if not line.startswith("[fail]"):
                        break
                    if attempt <= len(_FAVORITES_ITEM_RETRY_DELAYS):
                        delay = _FAVORITES_ITEM_RETRY_DELAYS[attempt - 1]
                        reason = line.split("|", 1)[-1].strip()
                        lib.record_failure(item, reason)
                        item_progress.stage(
                            f"失败后即时重试 {attempt}/{len(_FAVORITES_ITEM_RETRY_DELAYS)}",
                            detail=f"{delay} 秒后重试：{reason}",
                            force=True,
                        )
                        print(
                            f"[retry] {item.get('aweme_id') or ''} | "
                            f"第 {attempt} 次失败，{delay} 秒后就地重试",
                            flush=True,
                        )
                        await asyncio.sleep(delay)
                print(line, flush=True)
                progress.record(line, item_progress.key)
                if line.startswith(("[ok]", "[skip]")):
                    aid = item.get("aweme_id") or ""
                    if aid and lib.known(aid):
                        confirmed.append(item)
                if line.startswith(("[ok]", "[warn]")):
                    ok += 1
                elif line.startswith("[skip]"):
                    skipped += 1
                else:
                    failed += 1
                queue.task_done()

        workers = [
            asyncio.create_task(worker())
            for _ in range(_FAVORITES_PIPELINE_WORKERS)
        ]
        await queue.join()
        await asyncio.gather(*workers)

        if confirmed and inventory_complete and not args.keep_collected:
            progress.stage("写入待取消收藏队列", force=True)
            queued = lib.queue_uncollect(confirmed)
            print(f"已写入待取消收藏队列 {len(queued)} 条：{lib.pending_uncollect_path}")
            progress.stage("处理待取消收藏队列", force=True)
            await _run_pending_uncollect(lib, force_now=args.force_uncollect)

        if failed:
            final_status = f"完成（{failed} 条失败）"
        elif inventory_complete:
            final_status = "完成"
        else:
            final_status = "完成（当前稳定可见部分，清点未到底）"
        progress.finish(final_status)
    except BaseException:
        progress.finish("中断")
        raise

    print(f"完成：入库 {ok}，跳过 {skipped}，失败 {failed}。待分类都在 {lib.inbox}")
    return 0 if failed == 0 else 1


def _select_visual_calibration(items: list[dict], lib: Library, count: int) -> list[dict]:
    """按图文/视觉/教程/口播/其它分桶抽样，避免20条全是同一种内容。"""
    import visual_assets as va

    pending = [
        i for i in items
        if not lib.known(i.get("aweme_id") or "")
        and (i.get("kind") == "image" or int(i.get("duration_ms") or 0) <= 10 * 60 * 1000)
    ]
    buckets = {"image": [], "visual": [], "tutorial": [], "spoken": [], "other": []}
    for item in pending:
        if item.get("kind") == "image":
            buckets["image"].append(item)
            continue
        text = (item.get("desc") or "").lower()
        if any(w.lower() in text for w in va.TUTORIAL_WORDS):
            buckets["tutorial"].append(item)
        elif any(w.lower() in text for w in va.VISUAL_WORDS):
            buckets["visual"].append(item)
        elif any(w.lower() in text for w in va.SPOKEN_WORDS):
            buckets["spoken"].append(item)
        else:
            buckets["other"].append(item)

    # 每桶内部覆盖短/中/较长，但校准阶段单条最长 10 分钟，避免样本被长视频拖垮。
    for rows in buckets.values():
        rows.sort(key=lambda i: int(i.get("duration_ms") or 0))
    quotas = {"image": 4, "visual": 4, "tutorial": 4, "spoken": 4, "other": 4}
    selected: list[dict] = []
    used: set[str] = set()
    for name, quota in quotas.items():
        rows = buckets[name]
        take = min(quota, len(rows), max(count - len(selected), 0))
        if not take:
            continue
        if name == "image":
            indexes = {round(i * (len(rows) - 1) / max(take - 1, 1)) for i in range(take)}
            picked = [rows[idx] for idx in sorted(indexes)]
        else:
            targets = [20, 60, 180, 420][:take]
            pool = list(rows)
            picked = []
            for target in targets:
                row = min(pool, key=lambda i: abs(int(i.get("duration_ms") or 0) / 1000 - target))
                picked.append(row)
                pool.remove(row)
        for row in picked:
            if row["aweme_id"] not in used:
                selected.append(row)
                used.add(row["aweme_id"])

    if len(selected) < count:
        leftovers = [i for i in pending if i["aweme_id"] not in used]
        leftovers.sort(key=lambda i: int(i.get("duration_ms") or 0))
        need = min(count - len(selected), len(leftovers))
        selected.extend(leftovers[:need])
    return selected[:count]


async def cmd_visual_calibration(args, lib: Library, model: str) -> int:
    """生成多模态校准样本；保存资产但不入正式笔记、不取消收藏。"""
    import douyin_collects as dc
    import visual_assets as va

    try:
        info, items = await dc.fetch_favorites(require_exhausted=False)
    except Exception as exc:
        print(f"[fail] 无法读取全部收藏：{exc}")
        return 1
    count = max(1, min(int(args.visual_calibration), 50))
    selected = _select_visual_calibration(items, lib, count)
    print(
        f"视觉校准：从 {len(items)} 条收藏中分层抽取 {len(selected)} 条；"
        "只保存校准资产，不取消收藏。"
    )
    if not info.get("complete"):
        print("[warn] 网页端稳定停在当前数量但仍返回 has_more=1；仅用于校准抽样，不视为全量清单。")
    total_s = sum(float(i.get("duration_ms") or 0) / 1000 for i in selected)
    print(f"样本视频总时长 {_fmt_duration(total_s)}，预计本地处理 {_fmt_eta(total_s * 0.20)} 左右。")
    if args.dry_run:
        for item in selected:
            seconds = int(item.get("duration_ms") or 0) // 1000
            print(
                f"  {item['aweme_id']} {item.get('kind', 'unknown'):>7} "
                f"{seconds:>5}s  {_one_line(item.get('desc', ''), 52)}"
            )
        return 0
    assets_root = lib.root / "assets"
    manifests = []
    failed = 0
    for idx, item in enumerate(selected, 1):
        aid = item["aweme_id"]
        manifest_path = assets_root / aid / "manifest.json"
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifests.append(manifest)
                print(f"[{idx}/{len(selected)}] [skip] {aid} | 已有校准资产", flush=True)
                continue
            except (OSError, json.JSONDecodeError):
                pass
        try:
            if item.get("kind") == "image":
                manifest = await asyncio.to_thread(va.process_image_post, item, assets_root)
            elif item.get("kind") == "video":
                with tempfile.TemporaryDirectory(prefix="visual_calibration_") as tmp:
                    media = await server._download_douyin_media(item["url"], tmp, need="video")
                    audio = await server._download_douyin_media(item["url"], tmp, need="audio")
                    transcript = await asyncio.to_thread(server._transcribe_sync, audio, model)
                    manifest = await asyncio.to_thread(
                        va.process_video, item, media, assets_root, transcript, True
                    )
            else:
                print(f"[{idx}/{len(selected)}] [warn] {aid} | 类型 {item.get('kind')} 暂不做视觉校准", flush=True)
                failed += 1
                continue
        except Exception as exc:
            print(f"[{idx}/{len(selected)}] [fail] {aid} | {type(exc).__name__}: {str(exc)[:100]}", flush=True)
            failed += 1
            continue
        manifests.append(manifest)
        decision = manifest["classification"]
        print(
            f"[{idx}/{len(selected)}] [ok] {aid} | {decision['primary_value']} "
            f"{decision['confidence']:.0%} | {len(manifest.get('frames') or manifest.get('originals') or [])} 张",
            flush=True,
        )

    if manifests:
        report = va.write_calibration_report(manifests, lib.root)
        size = sum(p.stat().st_size for p in assets_root.rglob("*") if p.is_file())
        print(f"校准报告：{report}")
        print(f"本次/现有视觉资产占用 {size / 1024 / 1024:.1f} MB")
    print(f"校准完成：成功 {len(manifests)}，失败 {failed}；未取消任何收藏。")
    return 0 if failed == 0 else 1


async def cmd_collection(args, lib: Library, model: str) -> int:
    """把一个或多个收藏夹里的东西转成笔记，成功的从收藏夹里移除。"""
    import douyin_collects as dc

    total_ok = total_skip = total_fail = 0
    done_ids: list[str] = []

    for name in args.collection:
        try:
            info, items = await dc.fetch_collection(name)
        except Exception as exc:
            print(f"[fail] 收藏夹「{name}」：{exc}")
            total_fail += 1
            continue

        videos = [i for i in items if i.get("kind") == "video"]
        images = [i for i in items if i.get("kind") == "image"]
        texts = [i for i in items if i.get("kind") == "text"]
        print(f"收藏夹「{info['name']}」抓到 {len(items)}/{info['total']} 条"
              f"（视频 {len(videos)}，图文 {len(images)}，纯文字 {len(texts)}）")

        if args.dry_run:
            for i in items:
                kind = {"image": "图文", "text": "文字"}.get(
                    i.get("kind"), f"{i['duration_ms'] // 1000}s"
                )
                mark = "已入库" if lib.known(i["aweme_id"]) else "待转"
                print(f"  [{mark}] {i['aweme_id']} {kind:>6}  {_one_line(i['desc'], 46)}")
            continue

        for item in items:
            line = await _ingest_collected_item(
                item, lib, model, args.force, args.no_screen
            )
            print(line, flush=True)
            if line.startswith("[ok]") or line.startswith("[warn]") or line.startswith("[skip]"):
                done_ids.append(item["aweme_id"])
            if line.startswith("[ok]") or line.startswith("[warn]"):
                total_ok += 1
            elif line.startswith("[skip]"):
                total_skip += 1
            else:
                total_fail += 1

    if done_ids and not args.dry_run and not args.keep_collected:
        # 只取消**确认入库**的，失败的原样留在收藏夹里等下次
        urls = {i["aweme_id"]: i.get("url", "") for i in items}
        removed, missed = await dc.uncollect(done_ids, args.collection[-1], urls)
        print(f"已从收藏夹移除 {removed} 条" + (f"，{missed} 条没移掉" if missed else ""))

    print(f"完成：入库 {total_ok}，跳过 {total_skip}，失败 {total_fail}。"
          f"待分类都在 {lib.inbox}")
    return 0 if total_fail == 0 else 1


async def cmd_repair_favorite(args, lib: Library, model: str) -> int:
    """从当前收藏接口取目标作品的绑定媒体，单条重转且绝不取消收藏。"""
    import douyin_collects as dc

    target_id = str(args.repair_favorite)
    _, items = await dc.fetch_favorites(require_exhausted=False)
    item = next((row for row in items if row.get("aweme_id") == target_id), None)
    if not item:
        print(f"[fail] {target_id} | 当前已加载的收藏列表里没有找到该作品")
        return 1
    line = await _ingest_collected_item(
        item, lib, model, True, args.no_screen
    )
    print(line, flush=True)
    print("单条修复不会取消收藏。")
    return 0 if line.startswith(("[ok]", "[warn]")) else 1


async def cmd_audit_favorites(args, lib: Library) -> int:
    """只比较收藏接口与本地元数据；正文仅在进程内做异常模式检测。"""
    import douyin_collects as dc
    import favorite_audit as fa

    if args.audit_report:
        snapshot_path = Path(args.audit_report)
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        items = snapshot.get("remote_items") or []
        liked = snapshot.get("liked_items") or []
        inventory = dict(snapshot.get("inventory") or {})
        print(f"从审计快照离线复算：{snapshot_path}；不会访问抖音。", flush=True)
    else:
        print("开始只读清点收藏并扫描本地元数据；不会转录，也不会取消收藏。", flush=True)
        # 审计比批处理更愿意等待尾页：连续 80 次无新增才停，尽量覆盖大收藏库。
        info, items = await dc.fetch_favorites(require_exhausted=False, stale_limit=80)
        liked = info.get("ignored_liked") or []
        inventory = {
            "count": len(items),
            "complete": bool(info.get("complete")),
            "has_more": info.get("has_more"),
            "responses": info.get("responses"),
            "liked_seen": len(liked),
        }
    items = [lib.apply_type_override(item) for item in items]
    liked = [lib.apply_type_override(item) for item in liked]
    local = fa.load_latest_index(lib.index_path)
    findings, stats = fa.audit_rows(items, local, liked)
    report_dir = Path(__file__).resolve().parent / "audit" / "runs"
    md_path, json_path = fa.write_reports(
        report_dir, findings, stats, inventory, remote_items=items, liked_items=liked
    )
    print(
        f"审计完成：收藏 {stats['remote']}，匹配本地 {stats['matched']}，"
        f"明确错误 {stats['confirmed']}，人工复核 {stats['review']}。"
    )
    print(f"报告：{md_path}")
    print(f"机器结果：{json_path}")
    if args.audit_apply:
        quarantine_dir, moved = fa.quarantine_confirmed(lib.root, findings, lib.index_path)
        if moved:
            cmd_sync(lib)
            print(f"已将 {moved} 条明确错误移到可恢复隔离区：{quarantine_dir}")
        else:
            print("没有可隔离的明确错误，未改动本地库。")
    else:
        print("本轮为预览；未移动任何文件。确认规则稳定后可加 --audit-apply。")
    print("批量转录保持暂停；未取消任何收藏。")
    return 0


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
    if args.login:
        import douyin_browser
        print("已打开抖音专用登录窗口，请扫码或完成验证（最多等待 5 分钟）。")
        ok = await douyin_browser.login(timeout_s=300)
        print("登录成功，状态已保存。" if ok else "登录未完成或窗口已关闭。")
        return 0 if ok else 1
    if args.list:
        return cmd_list(lib_only)
    if args.sync:
        return cmd_sync(lib_only)
    if args.classify:
        return cmd_classify(lib_only, args.apply, args.recategorize, args.brief)

    if args.collections:
        import douyin_collects as dc
        for f in await dc.list_collections():
            print(f"{f['total']:>5}  {f['name']}")
        return 0

    if args.uncollect_pending:
        return await _run_pending_uncollect(lib_only, force_now=args.force_uncollect)

    model = args.model or server.recommended_model()
    if args.visual_calibration:
        print(f"库 {lib_only.root} | 校准模型 {model} | 设备 {server.device_label()}")
        return await cmd_visual_calibration(args, lib_only, model)
    if args.repair_favorite:
        print(f"库 {lib_only.root} | 单条收藏修复 {args.repair_favorite} | 模型 {model}")
        return await cmd_repair_favorite(args, lib_only, model)
    if args.audit_favorites:
        return await cmd_audit_favorites(args, lib_only)
    if args.favorites:
        print(f"库 {lib_only.root} | 模型 {model} | 设备 {server.device_label()}")
        return await cmd_favorites(args, lib_only, model)
    if args.collection:
        print(f"库 {lib_only.root} | 模型 {model} | 设备 {server.device_label()}")
        return await cmd_collection(args, lib_only, model)

    urls = read_urls(args)
    if not urls:
        print("没有输入链接。用法：python ingest.py <链接> [更多链接...]")
        return 2

    lib = lib_only
    print(f"库 {lib.root} | 模型 {model} | 设备 {server.device_label()} | 共 {len(urls)} 条")

    ok = skipped = failed = 0
    for url in urls:
        line = await ingest_one(url, lib, model, args.force, args.no_screen)
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
    parser.add_argument("--classify", action="store_true", help="按 categories.toml 预览分类结果")
    parser.add_argument("--apply", action="store_true", help="配合 --classify：把分类写进 frontmatter")
    parser.add_argument("--recategorize", action="store_true",
                        help="配合 --classify：无视已有 category 重算（改了规则表后用）")
    parser.add_argument("--brief", action="store_true",
                        help="配合 --classify：只打印每类条数，不逐条列（库大时必用）")
    parser.add_argument("--collection", action="append", metavar="夹子名",
                        help="转写这个抖音收藏夹里的内容，可重复；成功的会从收藏夹移除")
    parser.add_argument("--collections", action="store_true", help="列出所有抖音收藏夹")
    parser.add_argument("--favorites", action="store_true",
                        help="处理抖音‘收藏’总列表全部内容，不需要收藏夹名")
    parser.add_argument("--repair-favorite", metavar="作品ID",
                        help="从收藏接口取指定作品的绑定媒体并强制重转；不会取消收藏")
    parser.add_argument("--audit-favorites", action="store_true",
                        help="只读比较当前收藏接口与本地索引，生成元数据异常报告")
    parser.add_argument("--audit-apply", action="store_true",
                        help="配合 --audit-favorites：把明确错误移到可恢复隔离区并重建索引")
    parser.add_argument("--audit-report", metavar="JSON快照",
                        help="配合 --audit-favorites：从已有机器报告离线复算，不访问抖音")
    parser.add_argument("--visual-calibration", type=int, nargs="?", const=20, metavar="数量",
                        help="分层抽取收藏生成视觉校准资产；默认20条，不取消收藏")
    parser.add_argument("--dry-run", action="store_true",
                        help="配合 --favorites/--collection：只清点，不转录、不取消")
    parser.add_argument("--keep-collected", action="store_true",
                        help="转完只入库，不加入待取消收藏队列")
    parser.add_argument("--max-items", type=int, metavar="数量",
                        help="只处理指定数量的尚未入库作品，用于小批量测速")
    parser.add_argument("--uncollect-pending", action="store_true",
                        help="只处理库根目录的 _待取消收藏.jsonl 队列")
    parser.add_argument("--force-uncollect", action="store_true",
                        help="即使预计超过 30 分钟也立即处理待取消收藏队列")
    parser.add_argument("--login", action="store_true",
                        help="打开可见的抖音专用浏览器，扫码或完成登录验证")
    parser.add_argument("--no-screen", action="store_true",
                        help="关掉画面兜底：默认转写稿相对时长过少时会抽帧 OCR 读画面")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
