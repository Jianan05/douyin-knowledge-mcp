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
ALL_FAVORITES = "__all_favorites__"

_SCROLL_ALL_JS = """async () => {
    const all = Array.from(document.querySelectorAll('*'));
    for (const el of all) {
        if (el.scrollHeight > el.clientHeight + 40) {
            el.scrollTop = Math.max(0, el.scrollHeight - el.clientHeight - 800);
        }
    }
    window.scrollBy(0, -800);
    await new Promise(resolve => setTimeout(resolve, 80));
    for (const el of all) {
        if (el.scrollHeight > el.clientHeight + 40) {
            el.scrollTop = el.scrollHeight;
        }
    }
    window.scrollTo(0, document.body.scrollHeight);
}"""

_LIST_PATH = "/aweme/v1/web/collects/list/"
_ITEM_PATH = "/aweme/v1/web/collects/video/list/"
_ALL_PATH = "/aweme/v1/web/aweme/listcollection/"
_LIKED_PATH = "/aweme/v1/web/aweme/favorite/"


def _post_kind(item: dict) -> str:
    """把收藏作品分成 video / image / text。

    纯文字帖没有稳定的 aweme_type；用“没有视频、没有图片、但有正文”兜底，
    比把所有未知类型都硬塞给 Whisper 安全。
    """
    if _is_image_post(item):
        return "image"
    video = item.get("video") or {}
    has_video = bool(
        video.get("play_addr") or video.get("bit_rate") or video.get("duration")
    )
    if has_video:
        return "video"
    if (item.get("desc") or "").strip():
        return "text"
    return "unknown"


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
    aweme_id = str(item.get("aweme_id") or "")
    kind = _post_kind(item)
    # 接口里的 iesdouyin 分享链接带临时签名和设备参数，过期后可能跳进推荐流，
    # 让“在抖音打开”显示成另一条作品。入库只保存由作品 ID 构造的稳定地址。
    route = "video" if kind == "video" else "note"
    share_url = f"https://www.douyin.com/{route}/{aweme_id}"
    return {
        "aweme_id": aweme_id,
        "desc": (item.get("desc") or "").strip(),
        "kind": kind,
        "is_image": kind == "image",  # 兼容旧调用方
        "duration_ms": video.get("duration") or 0,
        # 只在本轮内存队列中使用，不会写进笔记或 index。收藏接口给出的 video
        # 与 aweme_id 属于同一个对象，比重新打开会预载推荐流的作品页更可靠。
        "_video": video,
        "images": _image_urls(item),
        "url": share_url,
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
        self.saw_all_feed = False
        self.all_has_more: int | None = None
        self.all_cursor: int | str | None = None
        self.all_invalid_item_count = 0
        self.all_raw_item_count = 0
        self.all_feed_responses = 0
        self.ignored_liked: dict[str, dict] = {}

    def bucket(self, collects_id: str) -> dict[str, dict]:
        return self.buckets.setdefault(str(collects_id), {})

    def attach(self, page):
        page.on("response", self._on_response)

    def _add_aweme_items(
        self, body: dict, path: str, bucket_key: str, source_url: str
    ) -> None:
        bucket = self.bucket(bucket_key)
        for it in body.get("aweme_list") or []:
            row = _normalize(it)
            if row["aweme_id"]:
                # 下划线字段只用于诊断，不会写进转录稿或 index.jsonl。
                row["_source_path"] = path
                row["_source_url"] = source_url
                row["_is_ads"] = bool(it.get("is_ads"))
                row["_collect_stat"] = it.get("collect_stat")
                row["_user_digged"] = it.get("user_digged")
                row["_author_uid"] = str((it.get("author") or {}).get("uid") or "")
                bucket[row["aweme_id"]] = row

    def ingest_all_payload(self, body: dict, source_url: str = "") -> None:
        """统一吸收一页“全部收藏”响应及其分页元数据。"""
        if body.get("aweme_list") is None:
            return
        self.saw_all_feed = True
        self.all_feed_responses += 1
        if "has_more" in body:
            self.all_has_more = int(bool(body.get("has_more")))
        for cursor_key in ("cursor", "max_cursor", "min_cursor", "offset"):
            if cursor_key in body:
                self.all_cursor = body.get(cursor_key)
                break
        try:
            self.all_invalid_item_count = max(
                self.all_invalid_item_count,
                int(body.get("invalid_item_count") or 0),
            )
        except (TypeError, ValueError):
            pass
        self.all_raw_item_count += len(body.get("aweme_list") or [])
        self._add_aweme_items(body, _ALL_PATH, ALL_FAVORITES, source_url)
        self.last_has_more = body.get("has_more", 0)

    async def _on_response(self, resp):
        path = urlparse(resp.url).path
        # 只有 listcollection 是“收藏作品”总列表。
        # /aweme/favorite/ 虽然名字像收藏，实际返回“点赞”作品；
        # 它的 collect_stat=0, user_digged=1，绝对不能混入本地收藏库。
        if path not in (_LIST_PATH, _ITEM_PATH, _ALL_PATH, _LIKED_PATH):
            return
        try:
            body = await resp.json()
        except Exception:
            return
        if path == _LIKED_PATH:
            for it in body.get("aweme_list") or []:
                row = _normalize(it)
                if row["aweme_id"]:
                    self.ignored_liked[row["aweme_id"]] = row
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
        elif body.get("aweme_list") is not None:
            got = (parse_qs(urlparse(resp.url).query).get("collects_id") or [""])[0]
            # 指定收藏夹接口必须继续按 collects_id 分桶；其它收藏接口属于
            # “收藏”总列表。这样不会把收藏夹网格预加载的封面混进总列表。
            if path == _ALL_PATH:
                self.ingest_all_payload(body, resp.url)
            else:
                self._add_aweme_items(
                    body, path,
                    got if path == _ITEM_PATH else ALL_FAVORITES,
                    resp.url,
                )
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


async def fetch_favorites(
    max_scroll: int = 1000, allow_empty: bool = False, require_exhausted: bool = True,
    stale_limit: int = 20,
) -> tuple[dict, list[dict]]:
    """滚完“收藏”总列表，返回全部视频、图文和纯文字作品。

    只接受 listcollection 总列表接口；不能按 URL 里是否含
    favorite/collect 模糊判断，因为 /aweme/favorite/ 实际是点赞列表。
    """
    async with DouyinSession(headless=True) as session:
        page = await session.context.new_page()
        collector = _Collector()
        collector.attach(page)
        await page.goto(FAVORITE_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(7000)

        bucket = collector.bucket(ALL_FAVORITES)
        stale = 0
        seen_cursors: set[str] = set()
        for _ in range(max_scroll):
            before = len(bucket)
            before_responses = collector.all_feed_responses
            # 轻微上移再到底，让底部观察器在“本页只有无效占位、DOM 没变”
            # 时也能再次进入视口，由网页生成下一份合法签名请求。
            await page.evaluate(_SCROLL_ALL_JS)
            await page.mouse.wheel(0, 2500)
            for _wait in range(12):
                await page.wait_for_timeout(200)
                if (
                    len(bucket) > before
                    or collector.all_feed_responses > before_responses
                    or collector.all_has_more == 0
                ):
                    break
            cursor = (
                str(collector.all_cursor)
                if collector.all_cursor is not None
                else ""
            )
            cursor_advanced = bool(cursor) and cursor not in seen_cursors
            if cursor:
                seen_cursors.add(cursor)
            # 收到一页新响应本身就是进度；无效页可能不新增作品或改变游标。
            response_advanced = collector.all_feed_responses > before_responses
            progressed = len(bucket) > before or cursor_advanced or response_advanced
            stale = 0 if progressed else stale + 1
            if collector.all_has_more == 0:
                break
            effective_stale_limit = (
                max(stale_limit, 120)
                if collector.all_invalid_item_count
                else stale_limit
            )
            if stale >= effective_stale_limit:
                break

        title = await page.title()
        await page.close()
        if not collector.saw_all_feed or (not bucket and not allow_empty):
            raise DouyinBrowserError(
                STAGE_LOGIN,
                "收藏总列表没有抓到任何作品",
                "确认已登录且收藏不为空；若页面能看到作品，说明抖音改了收藏接口",
            )
        if collector.all_has_more == 1 and require_exhausted:
            raise DouyinBrowserError(
                STAGE_LOGIN,
                f"收藏只加载到 {len(bucket)} 条，抖音仍返回 has_more=1",
                "本次清点不完整，已拒绝继续处理；稍后重试或检查页面滚动结构",
            )
        items = list(bucket.values())
        return {
            "id": ALL_FAVORITES,
            "name": "全部收藏",
            "total": len(items),
            "page": title,
            "responses": collector.all_feed_responses,
            "has_more": collector.all_has_more,
            "complete": collector.all_has_more == 0,
            "cursor": collector.all_cursor,
            "invalid_item_count": collector.all_invalid_item_count,
            "raw_item_count": collector.all_raw_item_count,
            "ignored_liked": list(collector.ignored_liked.values()),
        }, items


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


async def _click_collect(page, aweme_id: str, url: str = "") -> bool:
    """打开视频页，点收藏按钮把它取消掉。

    ⚠️ 必须用 JS 直接 .click()：Playwright 的 page.click 会卡在可操作性检查上
    （元素被播放器盖住，实测必 timeout）。force=True 也不行。
    """
    target = url or f"https://www.douyin.com/video/{aweme_id}"
    await page.goto(target, wait_until="domcontentloaded")
    await page.wait_for_timeout(6000)
    try:
        await page.eval_on_selector(_COLLECT_BTN, "e => e.click()")
    except Exception:
        return False
    await page.wait_for_timeout(2500)
    return True


async def uncollect_items(
    items: list[dict], collection_name: str = "",
) -> tuple[list[str], list[str]]:
    """批量取消收藏，返回（确认移除 ID，仍在收藏 ID）。

    不信按钮的 class 状态（混淆类名，点完 DOM 还会重渲染）——
    点完之后**重新翻一遍夹子**，以夹子里还剩什么为准。这是唯一靠得住的验证。
    """
    rows = [i for i in items if i.get("aweme_id")]
    aweme_ids = [i["aweme_id"] for i in rows]
    if not rows:
        return [], []
    async with DouyinSession(headless=True) as session:
        page = await session.context.new_page()
        for item in rows:
            aid = item["aweme_id"]
            try:
                await _click_collect(page, aid, item.get("url") or "")
            except Exception:
                pass
            await asyncio.sleep(1.0)  # 别刷太快，容易招验证码
        await page.close()

    if not collection_name:
        try:
            _, left = await fetch_favorites(allow_empty=True)
        except Exception:
            # 点击已经执行，但无法二次清点时不能谎称“确认移除”。
            return [], aweme_ids
        still = {i["aweme_id"] for i in left}
        return [a for a in aweme_ids if a not in still], [a for a in aweme_ids if a in still]
    try:
        _, left = await fetch_collection(collection_name)
    except Exception:
        return [], aweme_ids
    still = {i["aweme_id"] for i in left}
    return [a for a in aweme_ids if a not in still], [a for a in aweme_ids if a in still]


async def uncollect(
    aweme_ids: list[str], collection_name: str = "", urls: dict[str, str] | None = None,
) -> tuple[int, int]:
    """兼容旧调用方：批量取消收藏，返回数量。"""
    removed, missed = await uncollect_items([
        {"aweme_id": aid, "url": (urls or {}).get(aid, "")} for aid in aweme_ids
    ], collection_name)
    return len(removed), len(missed)
