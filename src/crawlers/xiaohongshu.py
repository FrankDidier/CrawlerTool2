"""
小红书同城爬虫 — Playwright network-intercept scraping

Navigates to https://www.xiaohongshu.com/explore, intercepts
/api/sns/web/* API responses, and parses note items.
"""
import asyncio
import logging
from datetime import datetime
from .base import BaseCrawler, CrawlResult

logger = logging.getLogger(__name__)

EXPLORE_URL = "https://www.xiaohongshu.com/explore"
API_PATTERNS = (
    "/api/sns/web/v1/homefeed",
    "/api/sns/web/v1/feed",
    "/api/sns/web/v1/search",
    "/api/sns/web/v2/note",
    "/api/sns/web/v1/note",
)


class XiaohongshuCrawler(BaseCrawler):
    platform_name = "小红书"

    async def fetch_tongcheng(self) -> list[CrawlResult]:
        if not self.bm:
            return []
        try:
            page = await self.bm.get_page(self.platform_name)
        except Exception as exc:
            logger.warning("[小红书] Browser not available: %s", exc)
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
            await page.goto(EXPLORE_URL, wait_until="networkidle", timeout=30_000)

            # Try to click 附近 / 同城 filter if available
            try:
                local_tab = page.locator("text=附近").first
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
            logger.warning("[小红书] Page load: %s", exc)
        finally:
            page.remove_listener("response", on_response)

        await self.bm.save_cookies(self.platform_name)

        results: list[CrawlResult] = []
        seen: set[str] = set()
        for body in captured:
            results.extend(self._parse_response(body, seen))
        logger.info("[小红书] %d items from %d API responses", len(results), len(captured))
        return results

    # ── parsing ──

    def _parse_response(self, body: dict, seen: set) -> list[CrawlResult]:
        items: list[CrawlResult] = []
        data = body.get("data", {})

        if isinstance(data, dict):
            feed_items = data.get("items") or data.get("notes") or []
        elif isinstance(data, list):
            feed_items = data
        else:
            feed_items = []

        for entry in feed_items:
            r = self._parse_note(entry, seen)
            if r:
                items.append(r)
        return items

    def _parse_note(self, item: dict, seen: set) -> CrawlResult | None:
        if not isinstance(item, dict):
            return None

        note = item.get("note_card") or item.get("note") or item
        note_id = str(
            item.get("id", "")
            or note.get("note_id", "")
            or note.get("id", "")
        )
        if not note_id or note_id in seen:
            return None
        seen.add(note_id)

        user = note.get("user") or item.get("user") or {}
        nickname = user.get("nickname", "") or user.get("name", "")
        user_id = user.get("user_id", "") or user.get("userId", "")

        title = note.get("display_title", "") or note.get("title", "")
        desc = note.get("desc", "") or note.get("description", "")
        content = f"{title} {desc}".strip() if desc else title

        link = f"https://www.xiaohongshu.com/explore/{note_id}"

        ts = note.get("time", 0) or note.get("create_time", 0) or item.get("time", 0)
        pub_date = self._ts_to_str(ts)

        return CrawlResult(
            platform="小红书",
            item_id=note_id,
            nickname=nickname or str(user_id),
            content=content[:500],
            link=link,
            publish_date=pub_date,
        )

    @staticmethod
    def _ts_to_str(ts) -> str:
        if isinstance(ts, str):
            return ts[:16]
        if not ts:
            return ""
        try:
            if ts > 1e12:
                ts = ts / 1000
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return ""
