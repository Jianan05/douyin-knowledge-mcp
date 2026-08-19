"""
Douyin / Bilibili Video Analysis MCP Server

工具一览：
  - analyze_video(url)         : 一键转录（同步）。支持抖音和 Bilibili。
  - video_to_text(url)         : 异步任务，立即返回 job_id。适用于 Claude Desktop（硬超时）。
                                 后续用 get_transcript_result(job_id) 取结果。
  - get_transcript_result(...) : 轮询/等待异步任务结果。
  - download_video(url)        : 只下载视频，返回本地文件路径。
  - transcribe_video(file)     : 转录本地视频/音频文件。
  - analyze_douyin / douyin_to_text / download_douyin: 旧工具名，保留兼容。

Architecture:
  - Playwright (headless Chromium) intercepts aweme/detail API to get signed CDN URLs
  - yt-dlp extracts and downloads Bilibili media
  - urllib downloads direct Douyin/Bilibili media URLs where possible
  - faster-whisper transcribes (it internally uses ffmpeg on local files, which is fine)
"""

import asyncio
import functools
import json
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Video Analysis", log_level="ERROR")

_URL_RE = re.compile(
    r'https?://\S+|(?:www\.)?(?:v\.douyin\.com|douyin\.com|iesdouyin\.com|bilibili\.com|b23\.tv)/\S+',
    re.IGNORECASE,
)
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/16.0 Mobile/15E148 Safari/604.1"
)
_SSL_CTX = ssl.create_default_context()

# 下载和转录分开排队：长视频转录很慢，不能阻塞普通下载任务。
_DOWNLOAD_EXECUTOR = ThreadPoolExecutor(max_workers=3)
_TRANSCRIBE_EXECUTOR = ThreadPoolExecutor(max_workers=1)

# Whisper 模型：tiny=39MB/快，small=244MB/更准。
# 默认 tiny：抖音口播一般清晰，端到端 ~25s 可在 MCP 超时内完成。
# 如某段音频效果差，可在工具调用时传入 model_size="small"。
WHISPER_MODEL = "tiny"
_ALLOWED_MODELS = {"tiny", "base", "small", "medium", "large-v3"}
_DETAIL_RESPONSE_TIMEOUT = 30.0
_DETAIL_RESPONSE_RETRIES = 2
_BILIBILI_VIDEO_FORMAT = (
    "bv*[vcodec^=avc1][ext=mp4]+ba[ext=m4a]/"
    "bv*[vcodec^=avc1]+ba/"
    "bv*[ext=mp4]+ba[ext=m4a]/"
    "bv*+ba/"
    "b[vcodec^=avc1]/b"
)


# ── URL 提取 ──────────────────────────────────────────────

def _extract_url(text: str) -> str:
    """从纯URL或 App 分享文本中提取第一个URL。"""
    text = text.strip()
    m = _URL_RE.search(text)
    if not m:
        raise ValueError(f"输入中未找到有效URL: {text!r}")
    url = m.group(0)
    url = re.sub(r'[^\w./:?=&%-]+$', '', url)
    if not re.match(r"https?://", url, re.IGNORECASE):
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.netloc.lower() == "bilibili.com":
        url = parsed._replace(netloc="www.bilibili.com").geturl()
    return url


def _detect_platform(url: str) -> str:
    """Return the supported site name for a URL."""
    host = urlparse(url).netloc.lower()
    if "douyin.com" in host or "iesdouyin.com" in host:
        return "douyin"
    if "bilibili.com" in host or host.endswith("b23.tv"):
        return "bilibili"
    raise ValueError(f"暂不支持这个网站: {host or url}")


def _platform_label(platform: str) -> str:
    return {"douyin": "抖音", "bilibili": "Bilibili"}.get(platform, platform)


# ── Playwright：拦截 aweme/detail API ─────────────────────

async def _get_video_object(page_url: str) -> dict:
    """
    用 async Playwright 打开抖音页面，拦截 aweme/detail API，
    返回 aweme_detail.video 字典（包含 bit_rate 数组和 play_addr）。
    """
    last_error: Exception | None = None

    for attempt in range(_DETAIL_RESPONSE_RETRIES):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                ctx = await browser.new_context(
                    user_agent=_UA,
                    locale="zh-CN",
                    viewport={"width": 1280, "height": 720},
                    extra_http_headers={
                        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    },
                )
                page = await ctx.new_page()
                loop = asyncio.get_running_loop()
                detail_future = loop.create_future()
                parser_tasks = set()

                async def parse_detail_response(resp):
                    nonlocal last_error
                    try:
                        payload = await resp.json()
                    except Exception as e:
                        last_error = e
                        return
                    if payload.get("aweme_detail") and not detail_future.done():
                        detail_future.set_result(payload)

                def on_response(resp):
                    if detail_future.done():
                        return
                    if "aweme/v1/web/aweme/detail" not in resp.url:
                        return
                    task = asyncio.create_task(parse_detail_response(resp))
                    parser_tasks.add(task)
                    task.add_done_callback(parser_tasks.discard)

                page.on("response", on_response)
                try:
                    await page.goto(page_url, wait_until="commit", timeout=45000)
                except Exception as e:
                    last_error = e

                try:
                    payload = await asyncio.wait_for(
                        detail_future, timeout=_DETAIL_RESPONSE_TIMEOUT
                    )
                    return payload["aweme_detail"].get("video", {})
                except asyncio.TimeoutError as e:
                    last_error = e
                finally:
                    for task in parser_tasks:
                        task.cancel()
            finally:
                await browser.close()

        if attempt < _DETAIL_RESPONSE_RETRIES - 1:
            await asyncio.sleep(1)

    suffix = f" ({type(last_error).__name__})" if last_error else ""
    raise RuntimeError(f"Playwright 未能拦截到视频信息，请稍后重试{suffix}")


# ── 抖音分享页兜底：移动端 HTML 中的 window._ROUTER_DATA ─────

def _read_url_text_sync(url: str, headers: dict | None = None, timeout: int = 30) -> tuple[str, str]:
    request_headers = {
        "User-Agent": _MOBILE_UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.douyin.com/",
    }
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, "replace"), resp.geturl()


def _extract_json_object_after_marker(text: str, marker: str) -> dict:
    marker_pos = text.find(marker)
    if marker_pos < 0:
        raise ValueError(f"未找到 {marker}")
    start = text.find("{", marker_pos)
    if start < 0:
        raise ValueError(f"{marker} 后面没有 JSON 对象")

    depth = 0
    in_string = False
    escaped = False
    for idx, ch in enumerate(text[start:], start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:idx + 1])

    raise ValueError(f"{marker} JSON 对象不完整")


def _looks_like_douyin_video(value) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("play_addr"), dict)
        and any(key in value for key in ("bit_rate", "duration", "cover"))
    )


def _find_douyin_video_dict(value) -> dict | None:
    if isinstance(value, dict):
        if _looks_like_douyin_video(value):
            return value

        aweme = value.get("aweme_detail")
        if isinstance(aweme, dict) and _looks_like_douyin_video(aweme.get("video")):
            return aweme["video"]

        video = value.get("video")
        if _looks_like_douyin_video(video):
            return video

        for child in value.values():
            found = _find_douyin_video_dict(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_douyin_video_dict(child)
            if found:
                return found
    return None


def _douyin_share_page_candidates(original_url: str, final_url: str) -> list[str]:
    result = []

    def add(url: str) -> None:
        if url and url not in result:
            result.append(url)

    add(final_url)
    add(original_url)

    for url in (final_url, original_url):
        match = re.search(r"/(?:share/)?video/(\d+)", url)
        if not match:
            continue
        video_id = match.group(1)
        add(f"https://www.iesdouyin.com/share/video/{video_id}/")
        add(f"https://www.douyin.com/video/{video_id}")

    return result


def _get_video_object_from_share_page_sync(page_url: str) -> dict:
    errors: list[str] = []
    first_html = ""
    final_url = page_url
    try:
        first_html, final_url = _read_url_text_sync(page_url)
    except Exception as e:
        errors.append(f"{page_url}: {type(e).__name__}")

    fetched = {final_url: first_html} if first_html else {}
    for candidate in _douyin_share_page_candidates(page_url, final_url):
        try:
            html = fetched.get(candidate)
            if html is None:
                html, _ = _read_url_text_sync(candidate)
            data = _extract_json_object_after_marker(html, "window._ROUTER_DATA")
            video = _find_douyin_video_dict(data)
            if video:
                return video
            errors.append(f"{candidate}: 未找到 video 字段")
        except Exception as e:
            errors.append(f"{candidate}: {type(e).__name__}")

    detail = "; ".join(errors[-3:]) if errors else "没有可解析的分享页"
    raise RuntimeError(f"抖音分享页解析失败: {detail}")


async def _get_douyin_video_object(page_url: str) -> dict:
    loop = asyncio.get_running_loop()
    try:
        return await _get_video_object(page_url)
    except Exception as e:
        playwright_error = e

    try:
        return await loop.run_in_executor(
            _DOWNLOAD_EXECUTOR, _get_video_object_from_share_page_sync, page_url
        )
    except Exception as e:
        raise RuntimeError(
            f"抖音视频信息获取失败: Playwright 拦截 {playwright_error}; 分享页解析 {e}"
        ) from e


# ── 抖音：专用浏览器捕获媒体（主方案）────────────────────

_BASE_DIR = Path(__file__).resolve().parent
TRANSCRIPT_DIR = _BASE_DIR / "transcripts"


def _bundled_ffmpeg() -> str | None:
    """优先用 runtime 里 Playwright 自带的 ffmpeg，其次用系统 PATH 上的。"""
    root = _BASE_DIR / "runtime" / "ms-playwright"
    if root.is_dir():
        for child in sorted(root.glob("ffmpeg-*")):
            for name in ("ffmpeg-win64.exe", "ffmpeg-linux", "ffmpeg-mac"):
                exe = child / name
                if exe.is_file():
                    return str(exe)
    return shutil.which("ffmpeg")


def _av_stream_types(path: str) -> set[str]:
    """用 PyAV（faster-whisper 的依赖）判断文件里有哪些流，不依赖外部 ffprobe。"""
    try:
        import av
    except ImportError:
        return set()
    try:
        with av.open(path) as container:
            return {stream.type for stream in container.streams}
    except Exception:
        return set()


def _file_has_audio(path: str) -> bool:
    return "audio" in _av_stream_types(path)


def _file_has_video(path: str) -> bool:
    return "video" in _av_stream_types(path)


def _remux_hls_sync(url: str, out_path: str, headers: dict) -> None:
    """m3u8 用 ffmpeg 合流；请求头（含 Cookie）通过参数传入，不写日志。"""
    ffmpeg = _bundled_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("这条视频只有 m3u8 流，需要 ffmpeg 才能合并，但没找到可用的 ffmpeg")
    header_lines = "".join(
        f"{key}: {value}\r\n" for key, value in headers.items() if key.lower() != "host"
    )
    proc = subprocess.run(
        [ffmpeg, "-y", "-headers", header_lines, "-i", url, "-c", "copy", out_path],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError("m3u8 合并失败")


async def _capture_douyin_media(
    page_url: str,
    out_dir: str,
    need: str = "audio",
    on_progress=None,
) -> str:
    """
    用专用持久化浏览器打开页面，捕获媒体流并下载到本地，返回文件路径。

    need="audio": 转录用，要求文件里有音频流（优先纯音频候选）。
    need="video": 存档用，要求文件里有画面。
    """
    import douyin_browser as db

    loop = asyncio.get_running_loop()
    errors: list[str] = []

    async with db.DouyinSession(headless=True) as session:
        result = await session.capture(page_url)
        candidates = result.ordered()
        if need == "video":
            # 存档要画面，纯音频候选排到最后。
            candidates.sort(key=lambda c: 1 if c.kind == "audio" else 0)

        for index, cand in enumerate(candidates[:6]):
            suffix = ".m4a" if cand.kind == "audio" else ".mp4"
            out_path = os.path.join(out_dir, f"douyin_{need}_{index}{suffix}")
            headers = session.headers_for(cand.url)
            try:
                if cand.kind == "hls":
                    out_path = os.path.join(out_dir, f"douyin_{need}_{index}.mp4")
                    await loop.run_in_executor(
                        _DOWNLOAD_EXECUTOR,
                        functools.partial(_remux_hls_sync, cand.url, out_path, headers),
                    )
                else:
                    try:
                        await loop.run_in_executor(
                            _DOWNLOAD_EXECUTOR,
                            functools.partial(
                                _download_sync,
                                cand.url,
                                out_path,
                                headers=headers,
                                progress_cb=on_progress,
                            ),
                        )
                    except Exception as direct_error:
                        # 直连被拒时改用 browser context 自己的请求客户端。
                        errors.append(f"{cand.kind}/直连 {type(direct_error).__name__}")
                        await session.fetch_via_browser(cand.url, out_path)
            except Exception as exc:
                errors.append(f"{cand.kind}/{type(exc).__name__}")
                continue

            if need == "audio" and not _file_has_audio(out_path):
                errors.append(f"{cand.kind}/无音频流")
                continue
            if need == "video" and not _file_has_video(out_path):
                errors.append(f"{cand.kind}/无画面")
                continue

            if len(_LAST_CAPTURE_META) > 64:  # 只留最近的几十条，别让它无限长
                _LAST_CAPTURE_META.clear()
            _LAST_CAPTURE_META[out_path] = {
                "title": result.title,
                "video_id": result.video_id,
                "logged_in": result.logged_in,
            }
            return out_path

        detail = "; ".join(errors[-4:]) or "没有可用候选"
        stage = db.STAGE_DOWNLOAD
        hint = "登录态可能已过期，运行「登录抖音」重新扫码；或换一条链接"
        raise db.DouyinBrowserError(
            stage,
            f"捕获到 {len(candidates)} 条媒体候选，但都无法用于{'转录' if need == 'audio' else '存档'}（{detail}）",
            hint,
        )


# 下载文件 → 元数据（标题/视频 ID），供自动保存 TXT 用。
_LAST_CAPTURE_META: dict[str, dict] = {}


def capture_meta(path: str) -> dict:
    return _LAST_CAPTURE_META.get(path, {})


def _safe_filename(text: str, limit: int = 40) -> str:
    cleaned = re.sub(r'[\/:*?"<>|\r\n\t]+', "_", (text or "").strip())
    cleaned = re.sub(r"_{2,}", "_", cleaned).strip("_ .")
    return cleaned[:limit]


def save_transcript(
    text: str,
    url: str = "",
    title: str = "",
    platform: str = "",
    video_id: str = "",
) -> str:
    """把文字稿写成 UTF-8 TXT，返回绝对路径。"""
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    parts = [stamp]
    if platform:
        parts.append(platform)
    if video_id:
        parts.append(video_id)
    name_hint = _safe_filename(title)
    if name_hint:
        parts.append(name_hint)
    out_path = TRANSCRIPT_DIR / ("_".join(parts) + ".txt")
    header = []
    if title:
        header.append(f"标题：{title}")
    if url:
        header.append(f"链接：{url}")
    header.append(f"转录时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    body = "\n".join(header) + "\n" + "-" * 40 + "\n" + text.strip() + "\n"
    out_path.write_text(body, encoding="utf-8")
    return str(out_path)


# ── URL 选取（纯内存操作，无网络调用）────────────────────

def _external_cdn_urls(urls) -> list[str]:
    """Return public CDN URLs and drop internal douyin.com play endpoints."""
    raw = []
    if isinstance(urls, dict):
        for key in ("main_url", "backup_url", "url_list", "fallback_url"):
            value = urls.get(key)
            if isinstance(value, list):
                raw.extend(value)
            elif isinstance(value, str):
                raw.append(value)
    elif isinstance(urls, list):
        raw.extend(urls)
    elif isinstance(urls, str):
        raw.append(urls)

    result = []
    for url in raw:
        if not isinstance(url, str) or not url:
            continue
        if "douyin.com" in url:
            continue
        candidates = [url]
        if "/playwm/" in url:
            candidates.insert(0, url.replace("/playwm/", "/play/"))
        for candidate in candidates:
            if candidate not in result:
                result.append(candidate)
    return result


def _cdn_urls(item: dict) -> list[str]:
    """从 bit_rate 条目中提取外部 CDN URL（排除需要 cookie 的 douyin.com 内部地址）。"""
    return _external_cdn_urls(item.get("play_addr", {}).get("url_list", []))


def _sorted_audio_candidates(video: dict) -> list[tuple[int, int, str]]:
    """Return audio-only CDN candidates sorted by bitrate/size."""
    result = []
    for item in video.get("bit_rate_audio") or []:
        meta = item.get("audio_meta") or {}
        urls = _external_cdn_urls(meta.get("url_list") or {})
        if urls:
            result.append((
                meta.get("bitrate") or item.get("audio_quality") or 0,
                meta.get("size") or 0,
                urls[0],
            ))

    audio = video.get("audio") or {}
    for key in ("play_url", "play_addr"):
        value = audio.get(key) or {}
        urls = _external_cdn_urls(value.get("url_list", []))
        if urls:
            result.append((0, 0, urls[0]))

    result.sort(key=lambda item: (item[0], item[1]))
    return result


def _sorted_candidates(video: dict) -> list[tuple[int, str]]:
    """返回按码率升序排列的 (bit_rate, cdn_url) 列表。"""
    result = []
    for item in video.get("bit_rate") or []:
        urls = _cdn_urls(item)
        if urls:
            result.append((item.get("bit_rate", 0), urls[0]))
    result.sort()
    return result


def _sorted_progressive_candidates(video: dict) -> list[tuple[int, str]]:
    """返回普通 MP4 候选；DASH 候选通常是纯视频分片，不适合直接给 Whisper。"""
    result = []
    for item in video.get("bit_rate") or []:
        if item.get("format") != "mp4":
            continue
        urls = _cdn_urls(item)
        if urls:
            result.append((item.get("bit_rate", 0), urls[0]))
    result.sort()
    return result


def _pick_url_for_transcription(video: dict) -> str:
    """
    选取用于转录的最小音频/视频 URL。
    策略：优先使用 bit_rate_audio 中的音频流；没有音频流时再选择普通 MP4。
    DASH 视频候选常是纯视频分片，直接交给 Whisper/PyAV 会因为没有音频流而解码失败。
    """
    audio_cands = _sorted_audio_candidates(video)
    if audio_cands:
        return audio_cands[0][2]
    cands = _sorted_progressive_candidates(video)
    if cands:
        return cands[0][1]
    cands = _sorted_candidates(video)
    if cands:
        return cands[0][1]
    # 回退
    fallback = _external_cdn_urls((video.get("play_addr") or {}).get("url_list", []))
    if fallback:
        return fallback[0]
    raise RuntimeError("无法找到可用的视频链接")


def _pick_url_for_download(video: dict) -> str:
    """选取最高码率的外部 CDN URL（用于全质量下载）。"""
    cands = _sorted_progressive_candidates(video)
    if cands:
        return cands[-1][1]  # 最高码率的自包含 MP4
    cands = _sorted_candidates(video)
    if cands:
        return cands[-1][1]  # 最高码率
    fallback = _external_cdn_urls((video.get("play_addr") or {}).get("url_list", []))
    if fallback:
        return fallback[0]
    raise RuntimeError("无法找到可用的视频链接")


# ── Bilibili：yt-dlp 提取和下载 ──────────────────────────

def _load_ytdlp():
    try:
        from yt_dlp import YoutubeDL
    except ImportError as e:
        raise RuntimeError("Bilibili 支持需要安装 yt-dlp：pip install -r requirements.txt") from e
    return YoutubeDL


def _extract_bilibili_info(url: str) -> dict:
    """Extract Bilibili metadata and direct media URLs with yt-dlp."""
    YoutubeDL = _load_ytdlp()
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "skip_download": True,
        "http_headers": {"User-Agent": _UA},
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        message = str(e)
        lower = message.lower()
        if "geo-restricted" in lower or "may be deleted" in lower:
            raise RuntimeError(
                "Bilibili 无法解析这个视频：它可能已删除、地区限制、需要登录，"
                "或当前网络不可访问。请换一个公开可访问链接，或更新 yt-dlp 后重试。"
            ) from e
        raise RuntimeError(f"Bilibili 解析失败: {message}") from e

    entries = info.get("entries")
    if entries:
        first = next((entry for entry in entries if entry), None)
        if first:
            info = first
    return info


def _bilibili_headers(info: dict, fmt: dict | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": _UA,
        "Referer": "https://www.bilibili.com/",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": "https://www.bilibili.com",
    }
    for source in (info.get("http_headers") or {}, (fmt or {}).get("http_headers") or {}):
        for key, value in source.items():
            if value:
                headers[key] = value
    return headers


def _bilibili_bvid_from_url(url: str) -> str:
    match = re.search(r"(BV[0-9A-Za-z]+)", url)
    if not match:
        raise RuntimeError("Bilibili 链接中未找到 BV 号")
    return match.group(1)


def _bilibili_api_json_sync(url: str, headers: dict[str, str]) -> dict:
    text, _ = _read_url_text_sync(url, headers=headers)
    data = json.loads(text)
    if data.get("code") != 0:
        raise RuntimeError(f"Bilibili API 返回错误: {data.get('code')} {data.get('message')}")
    payload = data.get("data")
    if not isinstance(payload, dict):
        raise RuntimeError("Bilibili API 未返回 data 对象")
    return payload


def _bilibili_view_and_playurl_sync(url: str, qn: int = 16) -> tuple[dict, dict, str]:
    bvid = _bilibili_bvid_from_url(url)
    headers = _bilibili_headers({"http_headers": {"Referer": f"https://www.bilibili.com/video/{bvid}/"}})
    view = _bilibili_api_json_sync(
        f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
        headers,
    )
    cid = view.get("cid")
    if not cid and view.get("pages"):
        cid = view["pages"][0].get("cid")
    if not cid:
        raise RuntimeError("Bilibili API 未返回 cid")
    playurl = _bilibili_api_json_sync(
        f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn={qn}&fnval=16&fourk=1",
        headers,
    )
    return view, playurl, bvid


def _bilibili_media_urls(item: dict) -> list[str]:
    urls = []
    for key in ("baseUrl", "base_url"):
        value = item.get(key)
        if value:
            urls.append(value)
    for key in ("backupUrl", "backup_url"):
        value = item.get(key) or []
        if isinstance(value, str):
            value = [value]
        urls.extend(url for url in value if url)
    return list(dict.fromkeys(urls))


def _download_first_available(
    urls: list[str], out_path: str, headers: dict[str, str], progress_cb=None
) -> None:
    last_error: Exception | None = None
    for media_url in urls:
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
            _download_sync(media_url, out_path, headers=headers, progress_cb=progress_cb)
            return
        except Exception as e:
            last_error = e
    raise RuntimeError(f"所有媒体直链下载失败: {last_error}") from last_error


def _download_bilibili_transcription_media_via_api_sync(
    url: str, out_dir: str, progress_cb=None
) -> str:
    _, playurl, bvid = _bilibili_view_and_playurl_sync(url, qn=16)
    audio = (playurl.get("dash") or {}).get("audio") or []
    audio = [item for item in audio if _bilibili_media_urls(item)]
    if not audio:
        raise RuntimeError("Bilibili API 未返回可下载音频流")
    best_audio = max(audio, key=lambda item: int(item.get("bandwidth") or 0))
    headers = _bilibili_headers({"http_headers": {"Referer": f"https://www.bilibili.com/video/{bvid}/"}})
    out_path = os.path.join(out_dir, "bilibili_media.m4a")
    _download_first_available(_bilibili_media_urls(best_audio), out_path, headers, progress_cb)
    return out_path


def _is_direct_http_format(fmt: dict) -> bool:
    protocol = str(fmt.get("protocol") or "")
    return bool(fmt.get("url")) and protocol.startswith("http") and not fmt.get("fragments")


def _format_size(fmt: dict) -> int:
    return int(fmt.get("filesize") or fmt.get("filesize_approx") or 0)


def _pick_bilibili_transcription_format(info: dict) -> dict:
    formats = [fmt for fmt in info.get("formats", []) if _is_direct_http_format(fmt)]
    audio = [
        fmt for fmt in formats
        if fmt.get("vcodec") == "none" and fmt.get("acodec") not in (None, "none")
    ]
    combined = [
        fmt for fmt in formats
        if fmt.get("vcodec") not in (None, "none") and fmt.get("acodec") not in (None, "none")
    ]
    candidates = audio or combined
    if not candidates:
        raise RuntimeError("Bilibili 未返回可直接下载的音频/视频格式")
    return max(
        candidates,
        key=lambda fmt: (
            float(fmt.get("abr") or fmt.get("tbr") or 0),
            _format_size(fmt),
            int(fmt.get("quality") or 0),
        ),
    )


def _safe_extension(ext: str | None, default: str = "mp4") -> str:
    ext = (ext or default).lower().lstrip(".")
    if not re.fullmatch(r"[a-z0-9]{1,8}", ext):
        return default
    return ext


def _download_bilibili_transcription_media_sync(url: str, out_dir: str, progress_cb=None) -> str:
    if re.search(r"BV[0-9A-Za-z]+", url):
        try:
            return _download_bilibili_transcription_media_via_api_sync(url, out_dir, progress_cb)
        except Exception:
            pass
    try:
        info = _extract_bilibili_info(url)
        fmt = _pick_bilibili_transcription_format(info)
        ext = _safe_extension(fmt.get("ext"), "m4a")
        out_path = os.path.join(out_dir, f"bilibili_media.{ext}")
        _download_sync(fmt["url"], out_path, headers=_bilibili_headers(info, fmt), progress_cb=progress_cb)
        return out_path
    except Exception:
        return _download_bilibili_transcription_media_via_api_sync(url, out_dir, progress_cb)


def _probe_media_streams(path: str) -> list[dict] | None:
    if not shutil.which("ffprobe"):
        return None
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,duration",
            "-of",
            "json",
            path,
        ],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    streams = data.get("streams")
    return streams if isinstance(streams, list) else None


def _has_video_stream(path: str) -> bool | None:
    streams = _probe_media_streams(path)
    if streams is None:
        return None
    return any(stream.get("codec_type") == "video" for stream in streams)


def _has_audio_stream(path: str) -> bool | None:
    streams = _probe_media_streams(path)
    if streams is None:
        return None
    return any(stream.get("codec_type") == "audio" for stream in streams)


def _ensure_audio_stream(path: str) -> None:
    has_audio = _has_audio_stream(path)
    if has_audio is False:
        raise RuntimeError(
            "下载完成，但转录媒体没有音频流。这个视频可能本身无声，"
            "也可能是平台返回了纯视频/DASH 分片；请更新后重试或换一个链接。"
        )


def _ensure_video_stream(path: str) -> None:
    has_video = _has_video_stream(path)
    if has_video is False:
        raise RuntimeError(
            "下载完成，但输出文件没有视频画面。请更新 yt-dlp/ffmpeg 后重试，或换 VLC 播放器验证。"
        )


def _find_downloaded_file(out_dir: str, before: set[str]) -> str:
    after = {
        os.path.join(out_dir, name)
        for name in os.listdir(out_dir)
        if os.path.isfile(os.path.join(out_dir, name))
    }
    created = sorted(after - before, key=lambda path: os.path.getmtime(path), reverse=True)
    video_created = [path for path in created if _has_video_stream(path) is True]
    if video_created:
        return video_created[0]
    mp4_created = [path for path in created if Path(path).suffix.lower() == ".mp4"]
    if mp4_created:
        return mp4_created[0]
    if created:
        return created[0]
    existing = sorted(after, key=lambda path: os.path.getmtime(path), reverse=True)
    if existing:
        return existing[0]
    raise RuntimeError("下载完成但未找到输出文件")


def _download_bilibili_video_via_api_sync(url: str, out_dir: str) -> str:
    _, playurl, bvid = _bilibili_view_and_playurl_sync(url, qn=64)
    dash = playurl.get("dash") or {}
    videos = [item for item in dash.get("video") or [] if _bilibili_media_urls(item)]
    audios = [item for item in dash.get("audio") or [] if _bilibili_media_urls(item)]
    if not videos or not audios:
        raise RuntimeError("Bilibili API 未返回可合并的 DASH 音视频流")

    avc_videos = [
        item for item in videos
        if str(item.get("codecs") or "").lower().startswith("avc1")
    ]
    video_pool = avc_videos or videos
    best_video = max(
        video_pool,
        key=lambda item: (
            int(item.get("height") or 0),
            int(item.get("bandwidth") or 0),
        ),
    )
    best_audio = max(audios, key=lambda item: int(item.get("bandwidth") or 0))

    headers = _bilibili_headers({"http_headers": {"Referer": f"https://www.bilibili.com/video/{bvid}/"}})
    video_path = os.path.join(out_dir, "bilibili_video.m4s")
    audio_path = os.path.join(out_dir, "bilibili_audio.m4s")
    out_path = os.path.join(out_dir, "bilibili_video.mp4")

    _download_first_available(_bilibili_media_urls(best_video), video_path, headers)
    _download_first_available(_bilibili_media_urls(best_audio), audio_path, headers)

    if not shutil.which("ffmpeg"):
        raise RuntimeError(
            "Bilibili API fallback 已下载音视频分片，但合并 MP4 需要 ffmpeg。"
            "请安装 ffmpeg 后重试，例如 winget install Gyan.FFmpeg。"
        )
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            video_path,
            "-i",
            audio_path,
            "-c",
            "copy",
            out_path,
        ],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 合并 Bilibili 音视频失败: {proc.stderr.strip()[-500:]}")
    _ensure_video_stream(out_path)
    return out_path


def _download_bilibili_video_sync(url: str, out_dir: str) -> str:
    if re.search(r"BV[0-9A-Za-z]+", url):
        try:
            return _download_bilibili_video_via_api_sync(url, out_dir)
        except Exception:
            pass
    YoutubeDL = _load_ytdlp()
    before = {
        os.path.join(out_dir, name)
        for name in os.listdir(out_dir)
        if os.path.isfile(os.path.join(out_dir, name))
    }
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "format": _BILIBILI_VIDEO_FORMAT,
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(out_dir, "%(title).100B [%(id)s].%(ext)s"),
        "windowsfilenames": True,
        "http_headers": {"User-Agent": _UA},
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        downloads = info.get("requested_downloads") or []
        for item in downloads:
            for key in ("filepath", "filename", "_filename"):
                path = item.get(key)
                if path and os.path.exists(path):
                    _ensure_video_stream(path)
                    return path
        path = _find_downloaded_file(out_dir, before)
        _ensure_video_stream(path)
        return path
    except Exception:
        return _download_bilibili_video_via_api_sync(url, out_dir)


# ── 下载（urllib，无 ffmpeg 网络调用）───────────────────

def _download_sync(
    video_url: str,
    out_path: str,
    headers: dict | None = None,
    progress_cb=None,
) -> None:
    """
    用 urllib 同步下载媒体文件。在线程池中调用。

    progress_cb: 可选回调，签名 progress_cb(downloaded_bytes, total_bytes_or_None)。
        total 来自 Content-Length，分块传输时可能为 None。回调被限流到约每
        0.2 秒一次，避免刷爆调用方的队列。
    """
    request_headers = {"User-Agent": _UA, "Referer": "https://www.douyin.com/"}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(
        video_url,
        headers=request_headers,
    )
    with urllib.request.urlopen(req, context=_SSL_CTX, timeout=90) as r:
        total = int(r.headers.get("Content-Length") or 0) or None
        downloaded = 0
        last_emit = 0.0
        with open(out_path, "wb") as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if progress_cb is not None:
                    now = time.monotonic()
                    if now - last_emit >= 0.2 or (total and downloaded >= total):
                        last_emit = now
                        progress_cb(downloaded, total)
    if progress_cb is not None:
        progress_cb(downloaded, total)
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError("下载失败：平台返回了空文件，请稍后重试或换一个链接。")


# ── 转录（faster-whisper，beam_size=1 加速）──────────────

DEFAULT_TERMS = "Agent, few-shot, RAG, prompt, LLM, embedding, token, fine-tune"

# ── 设备选择：有 N 卡就用 GPU，用不了就回落 CPU ─────────────

# (device, compute_type)；None 表示还没探测过。
_DEVICE_CHOICE: tuple[str, str] | None = None
_MODEL_CACHE: dict[tuple, object] = {}
_CUDA_ERROR_MARKS = ("cublas", "cudnn", "cuda", "dll", "gpu", "out of memory")


def _enable_cuda_dll_dirs() -> None:
    """
    把 pip 装的 nvidia cuBLAS/cuDNN DLL 目录加进 DLL 搜索路径。

    Windows 上 Python 3.8+ 不再从 PATH 找扩展模块依赖的 DLL，必须显式
    add_dll_directory，否则 ctranslate2 能认出显卡、却在第一次前向时报
    "Library cublas64_12.dll is not found"。
    """
    if os.name != "nt":
        return
    try:
        import nvidia
    except ImportError:
        return
    for root in nvidia.__path__:
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            bin_dir = os.path.join(root, name, "bin")
            if os.path.isdir(bin_dir):
                try:
                    os.add_dll_directory(bin_dir)
                except OSError:
                    pass


def _pick_device() -> tuple[str, str]:
    """返回 (device, compute_type)。设 WHISPER_DEVICE=cpu 可强制走 CPU。"""
    global _DEVICE_CHOICE
    if _DEVICE_CHOICE is not None:
        return _DEVICE_CHOICE
    if os.environ.get("WHISPER_DEVICE", "").lower() == "cpu":
        _DEVICE_CHOICE = ("cpu", "int8")
        return _DEVICE_CHOICE
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            _enable_cuda_dll_dirs()
            _DEVICE_CHOICE = ("cuda", "float16")
            return _DEVICE_CHOICE
    except Exception:
        pass
    _DEVICE_CHOICE = ("cpu", "int8")
    return _DEVICE_CHOICE


def _fall_back_to_cpu() -> None:
    """GPU 半路挂了（缺 DLL、显存不够）时永久降级 CPU，并丢掉已加载的模型。"""
    global _DEVICE_CHOICE
    _DEVICE_CHOICE = ("cpu", "int8")
    _MODEL_CACHE.clear()


def _load_model(model_size: str):
    """加载并缓存模型。medium/large 加载要好几秒，缓存能省掉每次转录的重复开销。"""
    from faster_whisper import WhisperModel

    device, compute_type = _pick_device()
    key = (model_size, device, compute_type)
    model = _MODEL_CACHE.get(key)
    if model is None:
        # 只留一个：显存和内存都不宽裕，换模型时把上一个放掉。
        _MODEL_CACHE.clear()
        model = WhisperModel(model_size, device=device, compute_type=compute_type)
        _MODEL_CACHE[key] = model
    return model


def device_label() -> str:
    """给界面显示的设备说明。"""
    device, compute_type = _pick_device()
    return "GPU (CUDA/float16)" if device == "cuda" else f"CPU ({compute_type})"

# Whisper 的 initial_prompt 只吃约 224 个 token，术语表太长会被从头截掉，
# 所以这里限一个长度，宁可少放几个也别把整段提示挤没。
_MAX_TERMS_CHARS = 300


def build_initial_prompt(terms: str = "") -> str:
    """
    组装 initial_prompt。

    英文专业名词（Agent / few-shot / RAG…）靠这里解决：实测同一条音频，
    不给术语时 base 输出 "AZN"、"FiuShout"，给了之后直接变成 "Agent"、"few-shot"，
    换更大的模型反而没有这一步管用。中文成语和数字错则是模型大小的问题，
    prompt 修不了，只能上 small / medium。
    """
    prompt = "以下是普通话的句子，请用简体中文转写。"
    terms = (terms or "").strip()
    if terms:
        prompt += f"内容可能涉及这些术语：{terms[:_MAX_TERMS_CHARS]}。"
    return prompt


def _transcribe_segments_sync(
    file_path: str,
    model_size: str = WHISPER_MODEL,
    on_segment=None,
    terms: str = DEFAULT_TERMS,
) -> str:
    """
    用 faster-whisper 转录视频/音频文件，返回完整文字稿。

    beam_size=1（贪心解码）比 beam_size=5 快。语言交给 Whisper 自动识别，
    避免英文口播被强制按中文解码。

    on_segment: 可选回调，每解码完一段就调用一次，签名为
        on_segment(segment_end_seconds, total_audio_seconds, text_so_far)。
        用于在 Web UI 里显示真实进度百分比和流式文字。faster-whisper 的
        segments 是惰性生成器，遍历它本身就是在做转录，所以回调能给出
        随转录推进的增量进度。
    """
    if model_size not in _ALLOWED_MODELS:
        model_size = WHISPER_MODEL
    model = _load_model(model_size)
    # initial_prompt 用简体中文示例：Whisper 默认常输出繁体，抖音语料后续要进
    # RAG/摘要，统一成简体更省事。英文口播不受影响（语言仍然自动识别）。
    # beam_size=5 + VAD：实测同一条 5.5 分钟音频，base 从 25s 涨到 47s，
    # 但「面试观→面试官」「调做工具→调错工具」这类错一批一批地掉，很划算。
    # VAD 还能跳过静音段，顺带压掉 Whisper 在无声处的幻觉重复。
    # initial_prompt 用简体中文并带上常见术语：Whisper 默认爱输出繁体，
    # 抖音语料后续要进 RAG/摘要，统一简体更省事。英文口播不受影响。
    def decode(active_model):
        return active_model.transcribe(
            file_path,
            beam_size=5,
            vad_filter=True,
            initial_prompt=build_initial_prompt(terms),
        )

    def collect(segments, info) -> str:
        total = float(getattr(info, "duration", 0.0) or 0.0)
        parts: list[str] = []
        for seg in segments:
            text = seg.text.strip()
            if text:
                parts.append(text)
            if on_segment is not None:
                on_segment(float(seg.end or 0.0), total, "\n".join(parts))
        return "\n".join(parts)

    try:
        return collect(*decode(model))
    except RuntimeError as exc:
        # ctranslate2 能认出显卡，却可能到真正前向时才发现缺 DLL 或显存不够。
        # 这类错只在 GPU 上出现：降级 CPU 重跑一次，别把 DLL 名字甩给用户。
        if _pick_device()[0] != "cuda":
            raise
        if not any(mark in str(exc).lower() for mark in _CUDA_ERROR_MARKS):
            raise
        _fall_back_to_cpu()

    return collect(*decode(_load_model(model_size)))


def _transcribe_sync(
    file_path: str, model_size: str = WHISPER_MODEL, terms: str = DEFAULT_TERMS
) -> str:
    """转录并返回完整文字稿（无进度回调，供 MCP 工具使用）。"""
    return _transcribe_segments_sync(file_path, model_size, terms=terms)


# ── 通用平台流程 ─────────────────────────────────────────

async def _download_douyin_media(
    real_url: str, out_dir: str, need: str = "audio", on_progress=None
) -> str:
    """
    抖音媒体获取的统一入口。

    主方案：专用持久化浏览器（真实登录态）捕获页面里实际播放的媒体流。
    兜底：旧的 aweme/detail 拦截 + 分享页解析（无登录态时基本已失效，仅作保险）。
    两条都失败时把两边的原因一起抛出，方便区分是登录、验证码还是媒体获取的问题。
    """
    loop = asyncio.get_running_loop()
    try:
        return await _capture_douyin_media(
            real_url, out_dir, need=need, on_progress=on_progress
        )
    except Exception as browser_error:
        primary = browser_error

    try:
        video = await _get_douyin_video_object(real_url)
        dl_url = (
            _pick_url_for_transcription(video) if need == "audio" else _pick_url_for_download(video)
        )
        out_path = os.path.join(out_dir, f"douyin_legacy_{need}.mp4")
        await loop.run_in_executor(
            _DOWNLOAD_EXECUTOR,
            functools.partial(_download_sync, dl_url, out_path, progress_cb=on_progress),
        )
        if need == "audio" and not _file_has_audio(out_path):
            raise RuntimeError("旧方案下载到的文件没有音频流")
        if need == "video" and not _file_has_video(out_path):
            raise RuntimeError("旧方案下载到的文件没有画面")
        return out_path
    except Exception as legacy_error:
        raise RuntimeError(
            f"{primary}\n（旧方案兜底也失败：{legacy_error}）"
        ) from primary


async def _download_transcription_media(
    real_url: str, out_dir: str, on_progress=None
) -> tuple[str, str]:
    """
    Download the best media file for transcription and return (path, platform).

    on_progress: 可选回调 on_progress(downloaded_bytes, total_bytes_or_None)，
        在下载过程中实时回报字节进度（抖音、Bilibili 直链/音频流都支持）。
    """
    platform = _detect_platform(real_url)
    loop = asyncio.get_running_loop()
    if platform == "douyin":
        out_path = await _download_douyin_media(
            real_url, out_dir, need="audio", on_progress=on_progress
        )
        return out_path, platform
    if platform == "bilibili":
        out_path = await loop.run_in_executor(
            _DOWNLOAD_EXECUTOR,
            functools.partial(
                _download_bilibili_transcription_media_sync, real_url, out_dir, on_progress
            ),
        )
        await loop.run_in_executor(_DOWNLOAD_EXECUTOR, _ensure_audio_stream, out_path)
        return out_path, platform
    raise ValueError(f"暂不支持这个平台: {platform}")


async def _download_video_file(
    real_url: str, out_dir: str, on_progress=None
) -> tuple[str, str]:
    """
    Download the source video and return (path, platform).

    on_progress: 抖音单文件下载支持实时字节进度；Bilibili 走 DASH 分片合并，
        进度无法简单线性回报，这里忽略该回调。
    """
    platform = _detect_platform(real_url)
    loop = asyncio.get_running_loop()
    if platform == "douyin":
        out_path = await _download_douyin_media(
            real_url, out_dir, need="video", on_progress=on_progress
        )
        return out_path, platform
    if platform == "bilibili":
        out_path = await loop.run_in_executor(
            _DOWNLOAD_EXECUTOR, _download_bilibili_video_sync, real_url, out_dir
        )
        return out_path, platform
    raise ValueError(f"暂不支持这个平台: {platform}")


async def _transcribe_url_async(url: str, model_size: str = WHISPER_MODEL) -> tuple[str, str, float]:
    """Download media for a supported URL and transcribe it."""
    real_url = _extract_url(url)
    loop = asyncio.get_running_loop()
    with tempfile.TemporaryDirectory(prefix="video_transcribe_") as tmp:
        media_path, platform = await _download_transcription_media(real_url, tmp)
        size_mb = os.path.getsize(media_path) / 1024 / 1024
        transcript = await loop.run_in_executor(
            _TRANSCRIBE_EXECUTOR, _transcribe_sync, media_path, model_size
        )
    return transcript, platform, size_mb


async def _download_video_async(url: str) -> tuple[str, str, float]:
    """Download source video for a supported URL into a persistent temp directory."""
    real_url = _extract_url(url)
    out_dir = tempfile.mkdtemp(prefix="video_dl_")
    video_path, platform = await _download_video_file(real_url, out_dir)
    size_mb = os.path.getsize(video_path) / 1024 / 1024
    return video_path, platform, size_mb


# ── MCP 工具 ─────────────────────────────────────────────

@mcp.tool()
async def analyze_douyin(url: str, model_size: str = WHISPER_MODEL) -> str:
    """
    抖音/Bilibili 视频一键转录：下载适合转录的媒体 → Whisper 转录 → 返回文字稿。

    url: 抖音或 Bilibili 分享链接/分享文本（自动提取URL）。
         支持格式：
           - 纯URL:  https://v.douyin.com/43Hxli09K70/
           - 长URL:  https://www.douyin.com/video/7628423061288682112
           - Bilibili: https://www.bilibili.com/video/BV...
           - 分享文本: "5.33 复制打开抖音... https://v.douyin.com/xxx/"，自动提取URL
    model_size: Whisper 模型大小，默认 "tiny"（快）。
                若文字识别明显有误，可改用 "small"（更准但慢约 4x）。
                可选: tiny / base / small / medium / large-v3
    """
    try:
        transcript, platform, _ = await _transcribe_url_async(url, model_size)
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"转录失败: {e}"

    if not transcript.strip():
        return f"{_platform_label(platform)} 转录完成，但未检测到语音内容（视频可能没有人声）。"

    return transcript


@mcp.tool()
async def analyze_video(url: str, model_size: str = WHISPER_MODEL) -> str:
    """
    通用视频转文字：支持抖音和 Bilibili 链接，返回 Whisper 文字稿。
    参数同 analyze_douyin。
    """
    return await analyze_douyin(url, model_size)


@mcp.tool()
async def download_douyin(url: str) -> str:
    """
    下载抖音/Bilibili 视频，返回本地文件路径。
    文件保存在系统临时目录，不会自动清理，请手动删除。

    url: 抖音或 Bilibili 分享链接/分享文本（自动提取URL）。
    """
    try:
        out_path, _, _ = await _download_video_async(url)
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"下载失败: {e}"

    return out_path


@mcp.tool()
async def download_video(url: str) -> str:
    """
    通用视频下载：支持抖音和 Bilibili 链接，返回本地文件路径。
    参数同 download_douyin。
    """
    return await download_douyin(url)


# ── 异步任务模式（用于 Claude Desktop 等硬超时客户端）─────

# job_id -> {"status": "running"|"done"|"error", "stage": str, "result": str|None, "started": float}
_JOBS: dict[str, dict] = {}
# 已完成任务保留时长（秒），之后 GC
_JOB_TTL = 600


def _gc_jobs():
    """清理过期任务。"""
    now = time.time()
    expired = [jid for jid, j in _JOBS.items()
               if j["status"] != "running" and now - j.get("done_at", j["started"]) > _JOB_TTL]
    for jid in expired:
        _JOBS.pop(jid, None)


async def _full_pipeline_bg(job_id: str, url: str, model_size: str) -> None:
    """后台跑完整流程：URL 提取 → 下载 → 转录。"""
    job = _JOBS[job_id]
    loop = asyncio.get_event_loop()
    tmp_dir = None
    try:
        job["stage"] = "extracting_url"
        real_url = _extract_url(url)

        tmp_dir = tempfile.mkdtemp(prefix="douyin_job_")

        job["stage"] = "downloading"
        media_path, platform = await _download_transcription_media(real_url, tmp_dir)

        job["stage"] = "transcribing"
        text = await loop.run_in_executor(
            _TRANSCRIBE_EXECUTOR, _transcribe_sync, media_path, model_size
        )

        job["status"] = "done"
        job["stage"] = "done"
        job["result"] = text or f"（{_platform_label(platform)} 未检测到语音内容）"
    except Exception as e:
        job["status"] = "error"
        job["result"] = f"{type(e).__name__}: {e}"
    finally:
        job["done_at"] = time.time()
        # 清理临时文件
        if tmp_dir:
            try:
                for f in os.listdir(tmp_dir):
                    try:
                        os.remove(os.path.join(tmp_dir, f))
                    except OSError:
                        pass
                os.rmdir(tmp_dir)
            except OSError:
                pass


@mcp.tool()
async def douyin_to_text(url: str, model_size: str = WHISPER_MODEL) -> str:
    """
    【推荐 Claude Desktop 使用】抖音/Bilibili 视频转文字（异步）。
    立即返回 job_id（<1秒），后台执行下载+转录。
    随后请调用 get_transcript_result(job_id) 取结果（该工具会等待最多 25 秒，未完成请再次调用）。

    适用场景：Claude Desktop chat 等客户端 MCP 工具调用有硬超时（约 30-60 秒），
    无法承受完整流程（25-90 秒）的同步调用。

    url: 抖音或 Bilibili 分享链接/分享文本（自动提取URL）。
         支持: https://v.douyin.com/xxx/、https://www.douyin.com/video/xxx、
               https://www.bilibili.com/video/BV... 或整段分享文本
    model_size: Whisper 模型，默认 "tiny"（快）。准度不够时改 "small"。
    """
    _gc_jobs()
    job_id = uuid.uuid4().hex[:8]
    _JOBS[job_id] = {
        "status": "running",
        "stage": "queued",
        "result": None,
        "started": time.time(),
    }
    asyncio.create_task(_full_pipeline_bg(job_id, url, model_size))
    return (
        f"任务已启动 (job_id={job_id})。\n"
        f"请调用 get_transcript_result(\"{job_id}\") 获取文字稿。"
        f"该工具会等待最多 25 秒，若未完成请再次调用同一个 job_id。"
    )


@mcp.tool()
async def video_to_text(url: str, model_size: str = WHISPER_MODEL) -> str:
    """
    通用异步视频转文字：支持抖音和 Bilibili，返回 job_id。
    参数同 douyin_to_text。
    """
    return await douyin_to_text(url, model_size)


@mcp.tool()
async def get_transcript_result(job_id: str, wait_seconds: float = 25.0) -> str:
    """
    获取异步转录任务的结果。会等待最多 wait_seconds 秒（默认 25，建议保持）。
    若任务在等待窗口内完成，直接返回文字稿；否则返回当前状态，调用方应再次调用。

    job_id: douyin_to_text 返回的任务ID。
    wait_seconds: 最长等待秒数（默认 25，必须小于客户端超时）。
    """
    if job_id not in _JOBS:
        return f"未知或已过期的 job_id: {job_id}"

    deadline = time.time() + max(0.5, min(wait_seconds, 28.0))
    while time.time() < deadline:
        job = _JOBS.get(job_id)
        if not job:
            return f"未知或已过期的 job_id: {job_id}"
        if job["status"] != "running":
            break
        await asyncio.sleep(0.5)

    job = _JOBS.get(job_id)
    if not job:
        return f"任务结果已过期: {job_id}"

    elapsed = time.time() - job["started"]
    if job["status"] == "running":
        return (
            f"[进行中] 已运行 {elapsed:.0f}s，当前阶段: {job['stage']}。\n"
            f"请再次调用 get_transcript_result(\"{job_id}\")。"
        )
    if job["status"] == "error":
        result = job["result"]
        # 错误也保留一段时间供 debug，由 GC 清理
        return f"转录失败: {result}"

    # done — 返回结果后立即清理
    result = job["result"]
    _JOBS.pop(job_id, None)
    return result


@mcp.tool()
async def transcribe_video(file_path: str, model_size: str = WHISPER_MODEL) -> str:
    """
    转录本地视频或音频文件，返回文字稿。
    使用 faster-whisper（默认 tiny 模型，自动识别语言）。

    file_path: 本地视频/音频文件的绝对路径。
    model_size: Whisper 模型大小，默认 "tiny"。准度不够时可改为 "small"。
    """
    path = Path(file_path)
    if not path.exists():
        return f"文件不存在: {file_path}"
    if not path.is_file():
        return f"路径不是文件: {file_path}"

    loop = asyncio.get_event_loop()
    try:
        transcript = await loop.run_in_executor(
            _TRANSCRIBE_EXECUTOR, _transcribe_sync, str(path), model_size
        )
    except Exception as e:
        return f"转录失败: {e}"

    if not transcript.strip():
        return "转录完成，但未检测到语音内容。"

    return transcript


if __name__ == "__main__":
    mcp.run(transport="stdio")
