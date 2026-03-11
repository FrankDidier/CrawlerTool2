"""
抖音同城爬虫 — Playwright network-intercept scraping

Navigates to https://www.douyin.com, tries to activate the 同城 tab,
intercepts /aweme/v1/web/* API responses, and parses feed items.
"""
import asyncio
import logging
from datetime import datetime
from .base import BaseCrawler, CrawlResult

logger = logging.getLogger(__name__)

DOUYIN_URL = "https://www.douyin.com"
API_PATTERNS = (
    "/aweme/v1/web/tab/feed",
    "/aweme/v1/web/nearby",
    "/aweme/v1/web/general/search",
    "/aweme/v1/web/discover",
    "/aweme/v1/web/recommend",
    "/aweme/v1/web/channel/feed",
)


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
            if not any(p in response.url for p in API_PATTERNS):
                return
            if response.status != 200:
                return
            try:
                body = await response.json()
                captured.append(body)
            except Exception:
                pass

        page.on("response", on_response)
        try:
            await page.goto(DOUYIN_URL, wait_until="networkidle", timeout=30_000)

            # Try to click 同城 tab if visible
            try:
                tab = page.locator("text=同城").first
                if await tab.is_visible(timeout=5000):
                    await tab.click()
                    await asyncio.sleep(3)
            except Exception:
                pass

            for _ in range(3):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(2)
            await asyncio.sleep(1)
        except Exception as exc:
            logger.warning("[抖音] Page load: %s", exc)
        finally:
            page.remove_listener("response", on_response)

        await self.bm.save_cookies(self.platform_name)

        results: list[CrawlResult] = []
        seen: set[str] = set()
        for body in captured:
            results.extend(self._parse_response(body, seen))
        logger.info("[抖音] %d items from %d API responses", len(results), len(captured))
        return results

    # ── parsing ──

    def _parse_response(self, body: dict, seen: set) -> list[CrawlResult]:
        items: list[CrawlResult] = []

        aweme_list = body.get("aweme_list") or []
        if not isinstance(aweme_list, list):
            nested = body.get("data")
            if isinstance(nested, dict):
                aweme_list = nested.get("aweme_list") or nested.get("data") or []
            elif isinstance(nested, list):
                aweme_list = nested
            else:
                aweme_list = []

        for aweme in aweme_list:
            r = self._parse_aweme(aweme, seen)
            if r:
                items.append(r)
        return items

    def _parse_aweme(self, aweme: dict, seen: set) -> CrawlResult | None:
        if not isinstance(aweme, dict):
            return None

        aweme_id = str(aweme.get("aweme_id", "") or aweme.get("id", ""))
        if not aweme_id or aweme_id in seen:
            return None
        seen.add(aweme_id)

        author = aweme.get("author") or {}
        nickname = author.get("nickname", "") or author.get("name", "")
        content = aweme.get("desc", "") or aweme.get("title", "") or ""
        share_url = aweme.get("share_url", "") or f"https://www.douyin.com/video/{aweme_id}"

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
