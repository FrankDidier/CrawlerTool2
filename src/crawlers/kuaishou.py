"""
快手同城爬虫 — Playwright network-intercept scraping

Navigates to https://www.kuaishou.cn/samecity, intercepts GraphQL
responses from /graphql, and parses feed items.
"""
import asyncio
import logging
from datetime import datetime
from .base import BaseCrawler, CrawlResult

logger = logging.getLogger(__name__)

SAMECITY_URL = "https://www.kuaishou.cn/samecity"


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
            if "/graphql" not in response.url:
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
            await page.goto(SAMECITY_URL, wait_until="networkidle", timeout=30_000)
            for _ in range(3):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(2)
            await asyncio.sleep(1)
        except Exception as exc:
            logger.warning("[快手] Page load: %s", exc)
        finally:
            page.remove_listener("response", on_response)

        await self.bm.save_cookies(self.platform_name)

        results: list[CrawlResult] = []
        seen: set[str] = set()
        for body in captured:
            results.extend(self._parse_graphql(body, seen))
        logger.info("[快手] %d items from %d API responses", len(results), len(captured))
        return results

    # ── parsing ──

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

    def _parse_item(self, item: dict, seen: set) -> CrawlResult | None:
        if not isinstance(item, dict):
            return None

        item_id = str(
            item.get("id", "")
            or item.get("photoId", "")
            or item.get("photo", {}).get("id", "")
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
            or item.get("photo", {}).get("caption", "")
            or item.get("description", "")
            or ""
        )

        link = (
            item.get("webUrl", "")
            or item.get("shareUrl", "")
            or f"https://www.kuaishou.cn/short-video/{item_id}"
        )

        ts = item.get("timestamp", 0) or item.get("createTime", 0)
        pub_date = self._ts_to_str(ts)

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
