"""
快手爬虫 — 城市搜索 + 同城页 + GraphQL 拦截

When target_city is configured:
  1. Search "{city}" on kuaishou.cn → city-relevant content
  2. Navigate to /samecity for local feed
  3. Intercept /graphql for scroll content

Without target_city:
  1. Navigate to /samecity (or main feed as fallback)
  2. SSR extraction + GraphQL interception
"""
import asyncio
import json
import logging
from datetime import datetime
from urllib.parse import quote

from .base import BaseCrawler, CrawlResult

logger = logging.getLogger(__name__)

SAMECITY_URL = "https://www.kuaishou.cn/samecity"
FALLBACK_URL = "https://www.kuaishou.cn"


class KuaishouCrawler(BaseCrawler):
    platform_name = "快手"

    async def fetch_tongcheng(self) -> list[CrawlResult]:
        if not self.bm:
            return []
        try:
            page = await self.bm.get_page(self.platform_name)
        except Exception as exc:
            logger.warning("[快手] Browser not available: %s", exc)
            return []

        captured: list[dict] = []

        async def on_response(response):
            url = response.url
            if response.status != 200:
                return
            if "/graphql" not in url:
                return
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct:
                    body = await response.json()
                    captured.append(body)
                    logger.debug("[快手] API captured: %s", url[:150])
            except Exception:
                pass

        page.on("response", on_response)
        search_items: list[dict] = []
        ssr_items: list[dict] = []

        try:
            city = self.target_city

            # ── City-based search (when configured) ──
            if city:
                search_url = (
                    f"https://www.kuaishou.cn/search/video"
                    f"?searchKey={quote(city)}"
                )
                try:
                    await page.goto(
                        search_url, wait_until="domcontentloaded",
                        timeout=25_000)
                    await asyncio.sleep(4)
                    search_items = await self._extract_ssr(page)
                    logger.info("[快手] City search '%s': %d items",
                                city, len(search_items))
                    for _ in range(3):
                        await page.evaluate(
                            "window.scrollTo(0, document.body.scrollHeight)")
                        await asyncio.sleep(2)
                except Exception as exc:
                    logger.debug("[快手] Search error: %s", exc)

            # ── Same-city page ──
            try:
                await page.goto(
                    SAMECITY_URL, wait_until="domcontentloaded",
                    timeout=25_000)
            except Exception:
                logger.info("[快手] samecity unavailable, using main feed")
                await page.goto(
                    FALLBACK_URL, wait_until="domcontentloaded",
                    timeout=25_000)

            await asyncio.sleep(3)
            title = await page.title()
            logger.info("[快手] Page: '%s' @ %s", title, page.url)

            ssr_items = await self._extract_ssr(page)
            if ssr_items:
                logger.info("[快手] SSR: %d items", len(ssr_items))

            for _ in range(5):
                await page.evaluate(
                    "window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(2)
            await asyncio.sleep(1)

        except Exception as exc:
            logger.warning("[快手] Page error: %s", exc)
        finally:
            page.remove_listener("response", on_response)

        await self.bm.save_cookies(self.platform_name)

        results: list[CrawlResult] = []
        seen: set[str] = set()

        for item in search_items:
            r = self._parse_item(item, seen)
            if r:
                results.append(r)
        search_count = len(results)

        for item in ssr_items:
            r = self._parse_item(item, seen)
            if r:
                results.append(r)

        for body in captured:
            results.extend(self._parse_graphql(body, seen))

        logger.info(
            "[快手] Total: %d (search=%d, SSR=%d, captures=%d)",
            len(results), search_count, len(ssr_items), len(captured),
        )
        return results

    # ── SSR extraction ──

    async def _extract_ssr(self, page) -> list[dict]:
        try:
            raw = await page.evaluate("""() => {
                for (const id of ['__NEXT_DATA__', 'RENDER_DATA',
                                   '__INITIAL_STATE__']) {
                    const el = document.getElementById(id);
                    if (el && el.textContent && el.textContent.length > 10)
                        return el.textContent;
                }
                for (const k of ['__NEXT_DATA__', '__INITIAL_STATE__']) {
                    const v = window[k];
                    if (v && typeof v === 'object' && !v.nodeType)
                        return JSON.stringify(v);
                }
                const scripts = document.querySelectorAll('script[type="application/json"]');
                for (const s of scripts) {
                    if (s.textContent && s.textContent.length > 200) return s.textContent;
                }
                return null;
            }""")
            if not raw:
                return []
            data = json.loads(raw)
            return self._dig_feed_items(data)
        except Exception as exc:
            logger.debug("[快手] SSR error: %s", exc)
            return []

    def _dig_feed_items(self, obj, depth=0) -> list[dict]:
        if depth > 6:
            return []
        if isinstance(obj, list):
            valid = [
                i for i in obj
                if isinstance(i, dict) and (
                    "id" in i or "photoId" in i or "photo" in i
                ) and ("author" in i or "user" in i or "caption" in i)
            ]
            if valid:
                return valid
            out: list[dict] = []
            for item in obj:
                if isinstance(item, dict):
                    out.extend(self._dig_feed_items(item, depth + 1))
            return out
        if isinstance(obj, dict):
            for key in ("feeds", "list", "items", "pcFeeds", "feedList",
                        "videoList", "data"):
                val = obj.get(key)
                if isinstance(val, list) and val:
                    found = self._dig_feed_items(val, depth + 1)
                    if found:
                        return found
            out = []
            for val in obj.values():
                if isinstance(val, (dict, list)):
                    out.extend(self._dig_feed_items(val, depth + 1))
            return out
        return []

    # ── GraphQL parsing ──

    def _parse_graphql(self, body: dict, seen: set) -> list[CrawlResult]:
        items: list[CrawlResult] = []
        data = body.get("data", {})
        if not isinstance(data, dict):
            return items

        for key, val in data.items():
            if not isinstance(val, dict):
                continue
            feed_list = (
                val.get("list")
                or val.get("feeds")
                or val.get("items")
                or val.get("pcFeeds")
                or []
            )
            if not isinstance(feed_list, list):
                continue
            for entry in feed_list:
                r = self._parse_item(entry, seen)
                if r:
                    items.append(r)
        return items

    def _parse_item(self, item: dict, seen: set):
        if not isinstance(item, dict):
            return None

        photo = item.get("photo") or {}
        item_id = str(
            item.get("id", "")
            or item.get("photoId", "")
            or photo.get("id", "")
        )
        if not item_id or item_id in seen:
            return None
        seen.add(item_id)

        author = item.get("author", {}) or item.get("user", {}) or {}
        nickname = (
            author.get("name", "")
            or author.get("userName", "")
            or author.get("user_name", "")
        )

        content = (
            item.get("caption", "")
            or photo.get("caption", "")
            or item.get("description", "")
            or ""
        )
        if not content and not nickname:
            return None

        link = (
            item.get("webUrl", "")
            or item.get("shareUrl", "")
            or f"https://www.kuaishou.cn/short-video/{item_id}"
        )

        ts = (
            item.get("timestamp", 0)
            or item.get("createTime", 0)
            or photo.get("timestamp", 0)
            or photo.get("createTime", 0)
            or photo.get("create_time", 0)
        )
        pub_date = self._ts_to_str(ts) or datetime.now().strftime("%Y-%m-%d %H:%M")

        return CrawlResult(
            platform="快手",
            item_id=item_id,
            nickname=nickname,
            content=content[:500],
            link=link,
            publish_date=pub_date,
        )

    @staticmethod
    def _ts_to_str(ts) -> str:
        if not ts:
            return ""
        try:
            if ts > 1e12:
                ts = ts / 1000
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return ""
