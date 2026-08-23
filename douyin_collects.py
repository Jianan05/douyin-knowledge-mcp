"""抖音收藏夹抓取：列夹子、翻夹子内容、取消收藏。

为什么不直接调接口：抖音 web 接口要 a_bogus 签名，签名是页面 JS 现算的，
外部构造不出来。所以这里一律**驱动页面自己去请求**，我们只在旁边拦响应。
慢一点，但不会因为签名算法改版就整个失效。
"""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

from douyin_browser import DouyinSession, DouyinBrowserError, STAGE_LOGIN

FAVORITE_URL = "https://www.douyin.com/user/self?showTab=favorite_collection"

_SCROLL_ALL_JS = """() => {
    const all = Array.from(document.querySelectorAll('*'));
    for (const el of all) {
        if (el.scrollHeight > el.clientHeight + 40) {
            el.scrollTop = el.scrollHeight;
        }
    }
    window.scrollTo(0, document.body.scrollHeight);
}"""

_LIST_PATH = "/aweme/v1/web/collects/list/"
_ITEM_PATH = "/aweme/v1/web/collects/video/list/"


def _is_image_post(item: dict) -> bool:
    """图文的三个特征：aweme_type=68 / media_type=2 / images 非空。任一命中就算。"""
    if item.get("aweme_type") == 68 or item.get("media_type") == 2:
        return True
    return bool(item.get("images"))


def _image_urls(item: dict) -> list[str]:
    out = []
    for img in item.get("images") or []:
        urls = (img.get("url_list") or []) if isinstance(img, dict) else []
        if urls:
            out.append(urls[-1])  # 最后一个通常是最大尺寸
    return out


def _normalize(item: dict) -> dict:
    video = item.get("video") or {}
    return {
        "aweme_id": item.get("aweme_id") or "",
        "desc": (item.get("desc") or "").strip(),
        "is_image": _is_image_post(item),
        "duration_ms": video.get("duration") or 0,
        "images": _image_urls(item),
        "url": f"https://www.douyin.com/video/{item.get('aweme_id')}",
    }


class _Collector:
    """挂在 page 上收响应，按接口路径分桶。

    ⚠️ 必须按 collects_id 过滤：收藏夹网格会给**每个夹子**预加载几条封面内容，
    不过滤的话别的夹子的视频会混进来（踩过，「转文本」94 条抓出一堆小说和美食）。
    """

    def __init__(self):
        self.folders: dict[str, dict] = {}
        self.buckets: dict[str, dict[str, dict]] = {}
        self.last_has_more = 1

    def bucket(self, collects_id: str) -> dict[str, dict]:
        return self.buckets.setdefault(str(collects_id), {})

    def attach(self, page):
        page.on("response", self._on_response)

    async def _on_response(self, resp):
        path = urlparse(resp.url).path
        if path not in (_LIST_PATH, _ITEM_PATH):
            return
        try:
            body = await resp.json()
        except Exception:
            return
        if path == _LIST_PATH:
            for f in body.get("collects_list") or []:
                cid = str(f.get("collects_id") or "")
                if cid:
                    self.folders[cid] = {
                        "id": cid,
                        "name": f.get("collects_name") or "",
                        "total": f.get("total_number") or 0,
                    }
        else:
            got = (parse_qs(urlparse(resp.url).query).get("collects_id") or [""])[0]
            bucket = self.bucket(got)
            for it in body.get("aweme_list") or []:
                row = _normalize(it)
                if row["aweme_id"]:
                    bucket[row["aweme_id"]] = row
            self.last_has_more = body.get("has_more", 0)


_CLICK_TEXT_JS = """(wanted) => {
    const els = Array.from(document.querySelectorAll('span,div,a,p'));
    const t = els.find(e => e.children.length === 0
                         && e.innerText && e.innerText.trim() === wanted);
    if (!t) return false;
    (t.closest('a,div[class]') || t).click();
    return true;
}"""


async def _click_text(page, wanted: str, timeout_s: float = 30.0) -> bool:
    """页面是懒加载的，标签可能要等几秒才出来 —— 轮询着点，别一次点不到就报错。

    先用 Playwright 的定位器（会自己算可见性），失败再退回手写 JS。
    只用 JS 那版踩过坑：文字有时嵌在多层 span 里，叶子节点匹配不到。
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        try:
            loc = page.get_by_text(wanted, exact=True)
            count = await loc.count()
            for i in range(min(count, 8)):
                item = loc.nth(i)
                if await item.is_visible():
                    await item.click(timeout=3000)
                    return True
        except Exception:
            pass
        try:
            if await page.evaluate(_CLICK_TEXT_JS, wanted):
                return True
        except Exception:
            pass
        await page.wait_for_timeout(1000)
    return False


async def _open_favorites(page, collector: _Collector) -> None:
    await page.goto(FAVORITE_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(6000)
    if not await _click_text(page, "收藏夹"):
        raise DouyinBrowserError(
            STAGE_LOGIN, "页面上找不到「收藏夹」标签",
            "确认已登录，或抖音改版了页面结构",
        )
    await page.wait_for_timeout(5000)


async def list_collections() -> list[dict]:
    """列出所有收藏夹：[{id, name, total}]。"""
    async with DouyinSession(headless=True) as session:
        page = await session.context.new_page()
        collector = _Collector()
        collector.attach(page)
        await _open_favorites(page, collector)
        # 夹子多的时候要往下滚才会继续拉
        for _ in range(6):
            await page.mouse.wheel(0, 1500)
            await page.wait_for_timeout(1500)
        await page.close()
        return sorted(collector.folders.values(), key=lambda f: -f["total"])


async def fetch_collection(name: str, max_scroll: int = 80) -> tuple[dict, list[dict]]:
    """打开指定名字的收藏夹，滚到底，返回 (夹子信息, 条目列表)。"""
    async with DouyinSession(headless=True) as session:
        page = await session.context.new_page()
        collector = _Collector()
        collector.attach(page)
        await _open_favorites(page, collector)
        for _ in range(6):
            await page.mouse.wheel(0, 1500)
            await page.wait_for_timeout(1200)

        target = next((f for f in collector.folders.values() if f["name"] == name), None)
        if target is None:
            names = "、".join(sorted(f["name"] for f in collector.folders.values()))
            raise DouyinBrowserError(
                STAGE_LOGIN, f"没找到名叫「{name}」的收藏夹",
                f"现有夹子：{names}",
            )

        opened = await _click_text(page, name, timeout_s=20.0)
        if not opened:
            raise DouyinBrowserError(STAGE_LOGIN, f"点不开「{name}」这个夹子", "抖音可能改版了")
        await page.wait_for_timeout(4000)

        bucket = collector.bucket(target["id"])
        stale = 0
        for _ in range(max_scroll):
            before = len(bucket)
            # 夹子内容在内层面板里，滚窗口没用 —— 把页面上所有能滚的容器都推到底。
            await page.evaluate(_SCROLL_ALL_JS)
            await page.mouse.wheel(0, 2500)
            await page.wait_for_timeout(1800)
            stale = stale + 1 if len(bucket) == before else 0
            if stale >= 12:  # 连续 12 次没新东西，认定到底了
                break

        await page.close()
        return target, list(bucket.values())


# ── 取消收藏 ─────────────────────────────────────────────

_COLLECT_BTN = '[data-e2e="video-player-collect"]'


async def _click_collect(page, aweme_id: str) -> bool:
    """打开视频页，点收藏按钮把它取消掉。

    ⚠️ 必须用 JS 直接 .click()：Playwright 的 page.click 会卡在可操作性检查上
    （元素被播放器盖住，实测必 timeout）。force=True 也不行。
    """
    await page.goto(f"https://www.douyin.com/video/{aweme_id}", wait_until="domcontentloaded")
    await page.wait_for_timeout(6000)
    try:
        await page.eval_on_selector(_COLLECT_BTN, "e => e.click()")
    except Exception:
        return False
    await page.wait_for_timeout(2500)
    return True


async def uncollect(aweme_ids: list[str], collection_name: str = "") -> tuple[int, int]:
    """批量取消收藏，返回 (确认移除数, 没移掉数)。

    不信按钮的 class 状态（混淆类名，点完 DOM 还会重渲染）——
    点完之后**重新翻一遍夹子**，以夹子里还剩什么为准。这是唯一靠得住的验证。
    """
    if not aweme_ids:
        return 0, 0
    async with DouyinSession(headless=True) as session:
        page = await session.context.new_page()
        for aid in aweme_ids:
            try:
                await _click_collect(page, aid)
            except Exception:
                pass
            await asyncio.sleep(1.0)  # 别刷太快，容易招验证码
        await page.close()

    if not collection_name:
        return len(aweme_ids), 0
    try:
        _, left = await fetch_collection(collection_name)
    except Exception:
        return len(aweme_ids), 0
    still = {i["aweme_id"] for i in left}
    removed = sum(1 for a in aweme_ids if a not in still)
    return removed, len(aweme_ids) - removed
