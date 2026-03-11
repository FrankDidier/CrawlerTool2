"""
微信视频号同城爬虫 — Playwright network-intercept scraping

Navigates to https://channels.weixin.qq.com, intercepts feed API
responses, and parses content items.

NOTE: WeChat Channels has the most restrictive web access; this
crawler may return fewer results than other platforms.  Login via
QR code is typically required.
"""
import asyncio
import logging
from datetime import datetime
from .base import BaseCrawler, CrawlResult

logger = logging.getLogger(__name__)

CHANNELS_URL = "https://channels.weixin.qq.com"
API_PATTERNS = (
    "cgi-bin/mmfinderassistant",
    "/channels/",
    "finder",
    "feedlist",
    "finderFeed",
    "mmfinder",
)


class WechatCrawler(BaseCrawler):
    platform_name = "微信视频号"

    async def fetch_tongcheng(self) -> list[CrawlResult]:
        if not self.bm:
            return []
        try:
            page = await self.bm.get_page(self.platform_name)
        except Exception as exc:
            logger.warning("[微信视频号] Browser not available: %s", exc)
            return []

        captured: list[dict] = []

        async def on_response(response):
            if not any(p in response.url for p in API_PATTERNS):
                return
            if response.status != 200:
                return
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct or "javascript" in ct:
                    body = await response.json()
                    captured.append(body)
            except Exception:
                pass

        page.on("response", on_response)
        try:
            await page.goto(CHANNELS_URL, wait_until="networkidle", timeout=30_000)

            # Try to navigate to local feed
            try:
                local_tab = page.locator("text=同城").or_(page.locator("text=附近")).first
                if await local_tab.is_visible(timeout=3000):
                    await local_tab.click()
                    await asyncio.sleep(3)
            except Exception:
                pass

            for _ in range(3):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(2)
            await asyncio.sleep(1)
        except Exception as exc:
            logger.warning("[微信视频号] Page load: %s", exc)
        finally:
            page.remove_listener("response", on_response)

        await self.bm.save_cookies(self.platform_name)

        results: list[CrawlResult] = []
        seen: set[str] = set()
        for body in captured:
            results.extend(self._parse_response(body, seen))
        logger.info("[微信视频号] %d items from %d API responses", len(results), len(captured))
        return results

    # ── parsing ──

    def _parse_response(self, body: dict, seen: set) -> list[CrawlResult]:
        items: list[CrawlResult] = []

        data = body.get("data", {})
        feed_list: list = (
            (data.get("list") or []) if isinstance(data, dict)
            else body.get("feedList")
            or body.get("objectList")
            or body.get("list")
            or []
        )
        if not isinstance(feed_list, list):
            feed_list = []

        for entry in feed_list:
            r = self._parse_item(entry, seen)
            if r:
                items.append(r)
        return items

    def _parse_item(self, item: dict, seen: set) -> CrawlResult | None:
        if not isinstance(item, dict):
            return None

        obj = item.get("object") or item
        item_id = str(
            obj.get("id", "")
            or obj.get("objectId", "")
            or item.get("feedId", "")
            or item.get("id", "")
        )
        if not item_id or item_id in seen:
            return None
        seen.add(item_id)

        nickname = (
            obj.get("nickname", "")
            or (obj.get("author") or {}).get("nickname", "")
            or item.get("nickname", "")
        )
        content = (
            obj.get("description", "")
            or obj.get("desc", "")
            or obj.get("title", "")
            or item.get("description", "")
            or ""
        )
        link = item.get("shareUrl", "") or item.get("url", "") or ""

        ts = obj.get("createTime", 0) or item.get("createTime", 0)
        pub_date = self._ts_to_str(ts)

        return CrawlResult(
            platform="微信视频号",
            item_id=item_id,
            nickname=str(nickname),
            content=content[:500],
            link=link,
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
