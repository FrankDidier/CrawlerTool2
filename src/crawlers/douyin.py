"""
抖音爬虫 — 城市搜索 + 信息流

Strategy (when target_city is configured):
  1. Navigate to douyin.com/search/{city} search page
  2. Intercept search API responses for city-relevant videos
  3. Fall back to main feed if search returns nothing
  4. Scroll to load more content

Without target_city:
  Navigate to main feed, SSR extract + scroll intercept.
"""
import asyncio
import json
import logging
from datetime import datetime
from urllib.parse import unquote, quote

from .base import BaseCrawler, CrawlResult

logger = logging.getLogger(__name__)

DOUYIN_URL = "https://www.douyin.com"


class DouyinCrawler(BaseCrawler):
    platform_name = "抖音"

    async def fetch_tongcheng(self) -> list[CrawlResult]:
        if not self.bm:
            return []
        try:
            page = await self.bm.get_page(self.platform_name)
        except Exception as exc:
            logger.warning("[抖音] Browser not available: %s", exc)
            return []

        captured: list[dict] = []

        async def on_response(response):
            url = response.url
            if response.status != 200:
                return
            if not any(k in url for k in (
                "/aweme/", "/nearby/", "/search/",
            )):
                return
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct or "javascript" in ct:
                    body = await response.json()
                    captured.append(body)
                    logger.debug("[抖音] Captured: %s (%d bytes)",
                                 url.split("?")[0][-60:],
                                 len(json.dumps(body, ensure_ascii=False)))
            except Exception:
                pass

        page.on("response", on_response)

        try:
            city = self.target_city

            if city:
                await self._do_city_search(page, city)
            else:
                await self._do_main_feed(page)

        except Exception as exc:
            logger.warning("[抖音] Page error: %s", exc)
        finally:
            page.remove_listener("response", on_response)

        await self.bm.save_cookies(self.platform_name)

        # Parse all captured API responses
        results: list[CrawlResult] = []
        seen: set[str] = set()

        for body in captured:
            parsed = self._parse_any_response(body, seen)
            results.extend(parsed)

        logger.info("[抖音] Total: %d items from %d API responses",
                    len(results), len(captured))
        return results

    # ── City search flow ──

    async def _do_city_search(self, page, city: str):
        """Navigate to search page, wait for results to load via API."""
        search_url = f"{DOUYIN_URL}/search/{quote(city)}?type=video"

        logger.info("[抖音] Navigating to search: %s", search_url)
        await page.goto(
            search_url, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(5)

        title = await page.title()
        logger.info("[抖音] Search page: '%s' @ %s", title, page.url)

        # Scroll to trigger more search API calls
        for i in range(5):
            await page.evaluate(
                "window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2.5)

        await asyncio.sleep(1)

    # ── Main feed flow (no city) ──

    async def _do_main_feed(self, page):
        """Navigate to main page, scroll for feed content."""
        await page.goto(
            DOUYIN_URL, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(5)

        title = await page.title()
        logger.info("[抖音] Main page: '%s' @ %s", title, page.url)

        for _ in range(5):
            await page.evaluate(
                "window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)
        await asyncio.sleep(1)

    # ── Universal response parser ──

    def _parse_any_response(self, body: dict, seen: set) -> list[CrawlResult]:
        """Parse any Douyin API response — feed, search, or nearby."""
        if not isinstance(body, dict):
            return []

        items: list[CrawlResult] = []
        awemes = self._extract_awemes(body)

        for aweme in awemes:
            r = self._parse_aweme(aweme, seen)
            if r:
                items.append(r)
        return items

    def _extract_awemes(self, obj, depth=0) -> list[dict]:
        """Recursively extract aweme objects from any response format.

        Handles:
          - Feed: {"aweme_list": [{aweme}, ...]}
          - Search: {"data": [{"aweme_info": {aweme}}, ...]}
          - Nested: {"data": {"data": [...]}}
        """
        if depth > 8:
            return []

        if isinstance(obj, list):
            out: list[dict] = []
            for item in obj:
                if not isinstance(item, dict):
                    continue
                # Search result wrapper: {"type": N, "aweme_info": {...}}
                ai = item.get("aweme_info")
                if isinstance(ai, dict) and (
                    "aweme_id" in ai or "desc" in ai
                ):
                    out.append(ai)
                    continue
                # Direct aweme object
                if "aweme_id" in item or "awemeId" in item:
                    out.append(item)
                    continue
                if "desc" in item and ("author" in item or "nickname" in item):
                    out.append(item)
                    continue
                # Recurse into nested structures
                out.extend(self._extract_awemes(item, depth + 1))
            return out

        if isinstance(obj, dict):
            # Check known list keys first
            for key in ("aweme_list", "awemeList", "data", "list",
                        "videoList", "video_list", "feedList",
                        "recommendList"):
                val = obj.get(key)
                if isinstance(val, list) and val:
                    found = self._extract_awemes(val, depth + 1)
                    if found:
                        return found
            # Recurse into all dict values
            out = []
            for val in obj.values():
                if isinstance(val, (dict, list)):
                    out.extend(self._extract_awemes(val, depth + 1))
            return out

        return []

    # ── Item parser ──

    def _parse_aweme(self, aweme: dict, seen: set):
        if not isinstance(aweme, dict):
            return None
        aweme_id = str(
            aweme.get("aweme_id", "")
            or aweme.get("awemeId", "")
            or aweme.get("id", "")
        )
        if not aweme_id or aweme_id in seen:
            return None
        seen.add(aweme_id)

        author = aweme.get("author") or aweme.get("authorInfo") or {}
        nickname = author.get("nickname", "") or author.get("name", "")
        content = aweme.get("desc", "") or aweme.get("title", "") or ""
        if not content and not nickname:
            return None

        share_url = (
            aweme.get("share_url", "")
            or aweme.get("shareUrl", "")
            or f"https://www.douyin.com/video/{aweme_id}"
        )

        ts = aweme.get("create_time", 0) or aweme.get("createTime", 0)
        pub_date = self._ts_to_str(ts)

        return CrawlResult(
            platform="抖音",
            item_id=aweme_id,
            nickname=nickname,
            content=content[:500],
            link=share_url,
            publish_date=pub_date,
        )

    @staticmethod
    def _ts_to_str(ts) -> str:
        if not ts:
            return ""
        try:
            return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return ""
