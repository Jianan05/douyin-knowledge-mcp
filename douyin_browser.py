"""
抖音专用浏览器：持久化登录态 + 真实页面媒体流捕获。

设计要点
--------
1. 专用 profile（``data/douyin-browser-profile``），和用户日常 Chrome/Edge 完全隔离，
   也不读取它们的 Cookie。用户只需首次在可见窗口里扫码登录一次，之后复用同一 profile。
2. 抓取时不再只等一个 API 名字，而是同时收集四路信号：
     - ``aweme/detail`` 等返回 JSON 结构的响应；
     - 响应 ``Content-Type`` 为 ``video/*`` ``audio/*`` 或 URL 形如 mp4/m4a/m3u8 的媒体响应；
     - 页面里 ``video.currentSrc`` / ``video.src``；
     - ``performance.getEntriesByType('resource')`` 里的媒体资源；
     - CDP ``Network.requestWillBeSent`` / ``responseReceived``（连 MSE 分片请求也能拿到）。
3. 下载复用同一个 browser context 的 Cookie / Referer / UA。Cookie 只在内存里传递，
   ⛔ 任何日志、异常信息都不会输出 Cookie 值。
4. 全程走系统证书校验，不做任何 TLS 降级，也不调用第三方解析站点。
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from urllib.parse import urlparse, urlsplit

from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "data" / "douyin-browser-profile"

# 登录态判定用的 Cookie 名（只看是否存在，不读取值）。
_LOGIN_COOKIE_NAMES = ("sessionid", "sessionid_ss", "sid_tt", "passport_auth_status")

_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# 把 Playwright 的自动化痕迹抹掉一部分：抖音对 navigator.webdriver 很敏感。
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
window.chrome = window.chrome || {runtime: {}};
"""

_MEDIA_EXT_RE = re.compile(r"\.(mp4|m4a|m4s|mp3|aac|m3u8|flv)(\?|$)", re.IGNORECASE)
_MEDIA_PATH_RE = re.compile(r"/(video|obj|aweme)/tos/|/tos-cn-|douyinvod|/media/", re.IGNORECASE)
_SKIP_EXT_RE = re.compile(r"\.(jpe?g|png|webp|gif|svg|css|js|woff2?|ico|json)(\?|$)", re.IGNORECASE)
# 静态资源域：播放器自带的占位视频、贴纸、字体等，不是内容媒体。
_STATIC_HOST_RE = re.compile(
    r"(douyinstatic|douyinpic|byteimg|bytecdn|effectcdn|byteeffect|sdk|log|mcs|monitor)\.",
    re.IGNORECASE,
)

# 阶段标签：出错时明确告诉用户卡在哪一层。
STAGE_LOGIN = "登录"
STAGE_CAPTCHA = "验证码"
STAGE_MEDIA = "媒体获取"
STAGE_DOWNLOAD = "下载"


class DouyinBrowserError(RuntimeError):
    """带阶段标记的抖音浏览器错误。"""

    def __init__(self, stage: str, message: str, hint: str = ""):
        self.stage = stage
        self.hint = hint
        full = f"[{stage}] {message}"
        if hint:
            full = f"{full}\n建议：{hint}"
        super().__init__(full)


class MediaCandidate:
    """一条候选媒体地址。"""

    __slots__ = ("url", "mime", "source", "kind", "size")

    def __init__(self, url: str, mime: str = "", source: str = "", size: int = 0):
        self.url = url
        self.mime = (mime or "").lower()
        self.source = source
        self.size = size
        self.kind = _classify(url, self.mime)

    def __repr__(self) -> str:  # pragma: no cover - 调试用，故意不打印查询串
        return f"<MediaCandidate {self.kind} from={self.source} host={urlparse(self.url).netloc}>"


def _classify(url: str, mime: str) -> str:
    lowered = url.lower()
    if mime.startswith("audio/") or re.search(r"\.(m4a|mp3|aac)(\?|$)", lowered):
        return "audio"
    if ".m3u8" in lowered or "mpegurl" in mime:
        return "hls"
    if mime.startswith("video/") or re.search(r"\.(mp4|m4s|flv)(\?|$)", lowered):
        return "video"
    return "unknown"


def _is_media_url(url: str, mime: str = "") -> bool:
    if not url or url.startswith(("blob:", "data:", "about:")):
        return False
    if not url.startswith(("http://", "https://")):
        return False
    if _STATIC_HOST_RE.search(urlparse(url).netloc):
        return False
    if _SKIP_EXT_RE.search(url) and not _MEDIA_EXT_RE.search(url):
        return False
    mime = (mime or "").lower()
    if mime.startswith(("video/", "audio/")) or "mpegurl" in mime:
        return True
    if _MEDIA_EXT_RE.search(url):
        return True
    if _MEDIA_PATH_RE.search(url) and "image" not in mime:
        return True
    return False


def _dedupe_key(url: str) -> str:
    """同一个媒体文件常带不同的 range/expire 参数，按 host+path 去重。"""
    parts = urlsplit(url)
    return f"{parts.netloc}{parts.path}"


def _extract_video_id(url: str) -> str:
    match = re.search(r"/video/(\d+)", url)
    return match.group(1) if match else ""


def _find_video_dict(value):
    """在任意 JSON 结构里找抖音的 video 字典（play_addr + bit_rate 特征）。"""
    if isinstance(value, dict):
        if isinstance(value.get("play_addr"), dict) and any(
            key in value for key in ("bit_rate", "duration", "cover")
        ):
            return value
        aweme = value.get("aweme_detail")
        if isinstance(aweme, dict):
            found = _find_video_dict(aweme.get("video"))
            if found:
                return found
        for child in value.values():
            found = _find_video_dict(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_video_dict(child)
            if found:
                return found
    return None


def _find_desc(value) -> str:
    if isinstance(value, dict):
        aweme = value.get("aweme_detail")
        if isinstance(aweme, dict) and isinstance(aweme.get("desc"), str):
            return aweme["desc"]
        for child in value.values():
            found = _find_desc(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_desc(child)
            if found:
                return found
    return ""


def _find_aweme_by_id(value, target_id: str) -> dict | None:
    """只返回目标作品自己的 aweme 字典，拒绝页面预载的推荐作品。"""
    if isinstance(value, dict):
        candidate_id = str(value.get("aweme_id") or value.get("group_id") or "")
        if candidate_id == target_id and isinstance(value.get("video"), dict):
            return value
        detail = value.get("aweme_detail")
        if isinstance(detail, dict):
            candidate_id = str(detail.get("aweme_id") or detail.get("group_id") or "")
            if candidate_id == target_id and isinstance(detail.get("video"), dict):
                return detail
        for child in value.values():
            found = _find_aweme_by_id(child, target_id)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_aweme_by_id(child, target_id)
            if found:
                return found
    return None


class CaptureResult:
    """一次抓取的产物：候选媒体 + 复用浏览器身份所需的请求头。"""

    def __init__(
        self,
        candidates: list[MediaCandidate],
        headers: dict[str, str],
        video_dict: dict | None,
        title: str,
        video_id: str,
        logged_in: bool,
        page_duration: float = 0.0,
    ):
        self.candidates = candidates
        self.headers = headers  # 含 Cookie，⛔ 不要打印
        self.video_dict = video_dict
        self.title = title
        self.video_id = video_id
        self.logged_in = logged_in
        # 页面播放器报的时长。抖音视频页会同时预载推荐位的其它视频，
        # 拿它当标尺才能识破「抓到隔壁视频」这种错。
        self.page_duration = page_duration

    def ordered(self) -> list[MediaCandidate]:
        """转录优先级：纯音频 → 渐进式 mp4 → HLS → 其它。"""
        order = {"audio": 0, "video": 1, "hls": 2, "unknown": 3}
        return sorted(self.candidates, key=lambda c: (order.get(c.kind, 9), -c.size))

    def safe_summary(self) -> str:
        """给日志/界面用的摘要，只有域名和类型，不含签名参数和 Cookie。"""
        rows = []
        for cand in self.ordered()[:8]:
            rows.append(f"{cand.kind}@{urlparse(cand.url).netloc}({cand.source})")
        return ", ".join(rows) or "无"


def profile_exists() -> bool:
    return PROFILE_DIR.exists() and any(PROFILE_DIR.iterdir())


class DouyinSession:
    """持久化 profile 的浏览器会话，异步上下文管理器。"""

    def __init__(self, headless: bool = True, slow_mo: int = 0):
        self.headless = headless
        self.slow_mo = slow_mo
        self._pw = None
        self.context = None

    async def __aenter__(self) -> "DouyinSession":
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        try:
            self.context = await self._pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=self.headless,
                # 必须用完整 Chromium（新版 headless），旧的 headless_shell 拿不到媒体：
                # 抖音在 headless shell 里根本不给 video 元素喂流（实测 readyState 一直是 0）。
                channel="chromium",
                slow_mo=self.slow_mo,
                user_agent=_DESKTOP_UA,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                viewport={"width": 1366, "height": 850},
                ignore_default_args=["--enable-automation"],
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--autoplay-policy=no-user-gesture-required",
                    "--mute-audio",
                ],
            )
        except Exception as exc:
            await self._shutdown()
            raise DouyinBrowserError(
                STAGE_LOGIN,
                f"无法启动 Playwright Chromium（{type(exc).__name__}）",
                "先运行 playwright install chromium；若使用可选 runtime，确认其中的 "
                "Chromium 完整，或关闭已经打开的同一个 profile 窗口后重试",
            ) from exc
        await self.context.add_init_script(_STEALTH_JS)
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self._shutdown()

    async def _shutdown(self) -> None:
        if self.context is not None:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None

    # ── 登录态 ───────────────────────────────────────────

    async def is_logged_in(self) -> bool:
        cookies = await self.context.cookies("https://www.douyin.com/")
        names = {c.get("name") for c in cookies}
        return any(name in names for name in _LOGIN_COOKIE_NAMES)

    async def interactive_login(self, timeout_s: float = 300.0) -> bool:
        """打开可见窗口让用户本人扫码/过验证。程序不接触密码。"""
        page = await self.context.new_page()
        await page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if await self.is_logged_in():
                await asyncio.sleep(2)  # 给 Cookie 落盘留点时间
                return True
            if page.is_closed():
                break
            await asyncio.sleep(2)
        return await self.is_logged_in()

    async def _request_headers(self, media_url: str, page_url: str, ua: str) -> dict[str, str]:
        """构造复用浏览器身份的请求头。返回值含 Cookie，调用方不得写日志。"""
        headers = {
            "User-Agent": ua or _DESKTOP_UA,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": page_url or "https://www.douyin.com/",
            "Origin": "https://www.douyin.com",
        }
        try:
            cookies = await self.context.cookies([media_url])
        except Exception:
            cookies = []
        pairs = [f"{c['name']}={c['value']}" for c in cookies if c.get("name")]
        if pairs:
            headers["Cookie"] = "; ".join(pairs)
        return headers

    # ── 媒体捕获 ─────────────────────────────────────────

    async def capture(self, page_url: str, wait_s: float = 25.0) -> CaptureResult:
        context = self.context
        page = await context.new_page()

        candidates: dict[str, MediaCandidate] = {}
        json_payloads: list[dict] = []
        captcha_seen = False
        page_duration = 0.0

        def add(url: str, mime: str = "", source: str = "", size: int = 0) -> None:
            if not _is_media_url(url, mime):
                return
            key = _dedupe_key(url)
            existing = candidates.get(key)
            if existing is None or (size and size > existing.size):
                candidates[key] = MediaCandidate(url, mime, source, size)

        pending: set[asyncio.Task] = set()

        async def read_json(resp) -> None:
            try:
                payload = await resp.json()
            except Exception:
                return
            if isinstance(payload, dict) and payload:
                json_payloads.append(payload)

        def on_response(resp) -> None:
            url = resp.url
            mime = (resp.headers or {}).get("content-type", "")
            size = 0
            try:
                size = int((resp.headers or {}).get("content-length") or 0)
            except (TypeError, ValueError):
                size = 0
            if _is_media_url(url, mime):
                add(url, mime, "response", size)
                return
            if "aweme" in url and ("detail" in url or "post" in url or "play" in url):
                task = asyncio.create_task(read_json(resp))
                pending.add(task)
                task.add_done_callback(pending.discard)

        page.on("response", on_response)

        # CDP：MSE 分片请求不一定走 response 事件，这里再兜一层。
        cdp = None
        try:
            cdp = await context.new_cdp_session(page)
            await cdp.send("Network.enable")

            def on_request_sent(event) -> None:
                request = event.get("request") or {}
                add(request.get("url", ""), event.get("type", ""), "cdp-request")

            def on_response_received(event) -> None:
                resp = event.get("response") or {}
                add(
                    resp.get("url", ""),
                    resp.get("mimeType", ""),
                    "cdp-response",
                    int(resp.get("encodedDataLength") or 0),
                )

            cdp.on("Network.requestWillBeSent", on_request_sent)
            cdp.on("Network.responseReceived", on_response_received)
        except Exception:
            cdp = None  # CDP 不可用不致命，其它三路照常

        try:
            await page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            if not candidates:
                raise DouyinBrowserError(
                    STAGE_MEDIA,
                    f"打开视频页面失败（{type(exc).__name__}）",
                    "确认链接在专用浏览器里能正常打开",
                ) from exc

        final_url = page.url
        ua = _DESKTOP_UA
        try:
            ua = await page.evaluate("() => navigator.userAgent") or _DESKTOP_UA
        except Exception:
            pass

        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            # 主动触发播放，让 MSE 真正开始请求分片。
            try:
                await page.evaluate(
                    """() => {
                        const v = document.querySelector('video');
                        if (v) { v.muted = true; const p = v.play(); if (p && p.catch) p.catch(() => {}); }
                    }"""
                )
            except Exception:
                pass

            # video.currentSrc / video.src
            try:
                info = await page.evaluate(
                    """() => Array.from(document.querySelectorAll('video'))
                        .map(v => ({
                            srcs: [v.currentSrc, v.src].filter(Boolean),
                            dur: isFinite(v.duration) ? v.duration : 0,
                            playing: !v.paused && v.readyState > 2,
                        }))"""
                )
                for entry in info or []:
                    for src in entry.get("srcs") or []:
                        add(src, "", "video-element")
                    # 取正在播放的那个播放器的时长；没有就取最长的。
                    dur = float(entry.get("dur") or 0)
                    if entry.get("playing") and dur > page_duration:
                        page_duration = dur
                    elif dur > page_duration and page_duration == 0:
                        page_duration = dur
            except Exception:
                pass

            # performance resource entries
            try:
                entries = await page.evaluate(
                    """() => performance.getEntriesByType('resource')
                        .map(e => [e.name, e.initiatorType, e.transferSize || 0])"""
                )
                for name, initiator, transfer in entries or []:
                    mime = "video/mp4" if initiator in ("video", "media") else ""
                    add(name, mime, "performance", int(transfer or 0))
            except Exception:
                pass

            if not captcha_seen:
                captcha_seen = await _looks_like_captcha(page)

            if any(c.kind in ("audio", "video", "hls") for c in candidates.values()):
                # 再多等一会儿，让更高码率/纯音频轨也被记录下来。
                await asyncio.sleep(2)
                break
            await asyncio.sleep(1.5)

        for task in list(pending):
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2)
            except Exception:
                task.cancel()

        logged_in = await self.is_logged_in()

        target_id = _extract_video_id(page_url)
        target_aweme = None
        for payload in json_payloads:
            target_aweme = _find_aweme_by_id(payload, target_id)
            if target_aweme:
                break
        if target_id and not target_aweme:
            raise DouyinBrowserError(
                STAGE_MEDIA,
                f"页面没有返回目标作品 {target_id} 的媒体信息，可能已跳到推荐视频",
                "保留失败记录并稍后重试；不要把当前页面里的其它视频当成目标作品",
            )

        video_dict = (target_aweme or {}).get("video") or None
        title = str((target_aweme or {}).get("desc") or "").strip()

        # 页面会预载推荐视频。只保留目标作品 JSON 明确给出的媒体 URL；宁可失败，
        # 也不能把推荐位的音频挂到收藏作品的 ID 下面。
        candidates.clear()
        for url in _urls_from_video_dict(video_dict):
            add(url, "", "aweme-target")

        target_duration = float((video_dict or {}).get("duration") or 0)
        if target_duration > 1000:
            target_duration /= 1000
        if target_duration > 0:
            page_duration = target_duration

        headers_by_url: dict[str, dict[str, str]] = {}
        result = CaptureResult(
            candidates=list(candidates.values()),
            headers={},
            video_dict=video_dict,
            title=title,
            video_id=target_id,
            logged_in=logged_in,
            page_duration=page_duration,
        )

        if not result.candidates:
            if captcha_seen:
                raise DouyinBrowserError(
                    STAGE_CAPTCHA,
                    "页面出现了验证码/滑块，抓取被拦下",
                    "运行「登录抖音」在可见窗口里手动通过验证，然后重试",
                )
            if not logged_in:
                raise DouyinBrowserError(
                    STAGE_LOGIN,
                    "没有捕获到任何媒体流，而且专用浏览器当前未登录",
                    "先运行「登录抖音」扫码登录一次",
                )
            raise DouyinBrowserError(
                STAGE_MEDIA,
                "已登录但没有捕获到媒体流；这条视频可能已删除、仅粉丝可见或本身没有视频",
                "在专用浏览器里手动打开同一条链接确认能否播放",
            )

        # 逐条准备请求头（Cookie 随域名不同而不同）。
        for cand in result.candidates:
            headers_by_url[cand.url] = await self._request_headers(cand.url, final_url, ua)
        result.headers = {}  # 不放全局 Cookie，避免误用
        self._headers_by_url = headers_by_url
        self._page_url = final_url
        self._ua = ua
        return result

    def headers_for(self, url: str) -> dict[str, str]:
        """取某条候选的请求头（含 Cookie，⛔ 不要打印）。"""
        cached = getattr(self, "_headers_by_url", {})
        if url in cached:
            return dict(cached[url])
        return {
            "User-Agent": getattr(self, "_ua", _DESKTOP_UA),
            "Referer": getattr(self, "_page_url", "https://www.douyin.com/"),
            "Accept": "*/*",
        }

    async def fetch_via_browser(self, url: str, out_path: str) -> int:
        """
        用 browser context 自带的 request 客户端下载（自动带 Cookie 和 TLS 校验）。
        直连被 403 时的兜底，不做流式进度。
        """
        headers = {
            k: v
            for k, v in self.headers_for(url).items()
            if k.lower() not in ("cookie", "host", "content-length")
        }
        resp = await self.context.request.get(url, headers=headers, timeout=120000)
        if not resp.ok:
            raise DouyinBrowserError(
                STAGE_DOWNLOAD,
                f"浏览器内下载被拒绝（HTTP {resp.status}）",
                "登录态可能已过期，重新运行「登录抖音」",
            )
        body = await resp.body()
        if not body:
            raise DouyinBrowserError(STAGE_DOWNLOAD, "浏览器内下载返回空文件")
        Path(out_path).write_bytes(body)
        return len(body)


def _urls_from_video_dict(video: dict | None) -> list[str]:
    """从 aweme 的 video 结构里取所有播放地址（含 douyin.com 内部地址）。"""
    if not isinstance(video, dict):
        return []
    found: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("url_list", "backup_url", "main_url", "fallback_url"):
                    if isinstance(value, str):
                        found.append(value)
                    elif isinstance(value, list):
                        found.extend(v for v in value if isinstance(v, str))
                else:
                    walk(value)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(video)
    seen: list[str] = []
    for url in found:
        if url.startswith("http") and url not in seen:
            seen.append(url)
    return seen


async def _looks_like_captcha(page) -> bool:
    try:
        return await page.evaluate(
            """() => {
                const text = document.body ? document.body.innerText || '' : '';
                if (/滑块|拖动|安全验证|验证码|captcha/i.test(text)) return true;
                return !!document.querySelector('#captcha_container, .captcha_verify_container, .vc-container');
            }"""
        )
    except Exception:
        return False


# ── 对外便捷入口 ─────────────────────────────────────────

async def login(timeout_s: float = 300.0) -> bool:
    """打开可见的专用浏览器，等用户本人完成登录。返回是否已登录。"""
    async with DouyinSession(headless=False, slow_mo=50) as session:
        return await session.interactive_login(timeout_s)


async def login_status() -> bool:
    """无界面地检查专用 profile 是否还有登录态。"""
    if not profile_exists():
        return False
    async with DouyinSession(headless=True) as session:
        return await session.is_logged_in()
