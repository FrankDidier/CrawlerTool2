"""
小红书爬虫 — 城市搜索 + 信息流 + 网络拦截

When target_city is configured:
  1. Search "{city}" on xiaohongshu.com → city-relevant notes
  2. Navigate to /explore for general feed
  3. Intercept /api/sns/web/* for scroll content

Without target_city:
  1. Navigate to /explore, try 附近/同城 tab
  2. SSR extraction + API interception
"""
import asyncio
import json
import logging
from datetime import datetime
from urllib.parse import quote

from .base import BaseCrawler, CrawlResult

logger = logging.getLogger(__name__)

EXPLORE_URL = "https://www.xiaohongshu.com/explore"


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
            url = response.url
            if response.status != 200:
                return
            if not any(p in url for p in (
                "/homefeed", "/feed", "/search", "/note",
            )):
                return
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct:
                    body = await response.json()
                    captured.append(body)
                    logger.debug("[小红书] API captured: %s", url[:150])
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
                    f"https://www.xiaohongshu.com/search_result"
                    f"?keyword={quote(city)}&type=1"
                )
                try:
                    await page.goto(
                        search_url, wait_until="domcontentloaded",
                        timeout=30_000)
                    await asyncio.sleep(4)
                    search_items = await self._extract_ssr(page)
                    logger.info("[小红书] City search '%s': %d items",
                                city, len(search_items))
                    for _ in range(3):
                        await page.evaluate(
                            "window.scrollTo(0, document.body.scrollHeight)")
                        await asyncio.sleep(2)
                except Exception as exc:
                    logger.debug("[小红书] Search error: %s", exc)

            # ── Explore page ──
            await page.goto(
                EXPLORE_URL, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(4)

            title = await page.title()
            logger.info("[小红书] Page: '%s' @ %s", title, page.url)

            ssr_items = await self._extract_ssr(page)
            if ssr_items:
                logger.info("[小红书] SSR: %d items", len(ssr_items))

            for _ in range(5):
                await page.evaluate(
                    "window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(2)
            await asyncio.sleep(1)

        except Exception as exc:
            logger.warning("[小红书] Page error: %s", exc)
        finally:
            page.remove_listener("response", on_response)

        await self.bm.save_cookies(self.platform_name)

        results: list[CrawlResult] = []
        seen: set[str] = set()

        for item in search_items:
            r = self._parse_note(item, seen)
            if r:
                results.append(r)
        search_count = len(results)

        for item in ssr_items:
            r = self._parse_note(item, seen)
            if r:
                results.append(r)

        for body in captured:
            results.extend(self._parse_response(body, seen))

        logger.info(
            "[小红书] Total: %d (search=%d, SSR=%d, captures=%d)",
            len(results), search_count, len(ssr_items), len(captured),
        )
        return results

    # ── SSR extraction ──

    async def _extract_ssr(self, page) -> list[dict]:
        try:
            raw = await page.evaluate("""() => {
                // Prefer script element textContent (most reliable)
                for (const id of ['__NEXT_DATA__', 'RENDER_DATA',
                                   '__INITIAL_SSR_DATA__']) {
                    const el = document.getElementById(id);
                    if (el && el.textContent && el.textContent.length > 10)
                        return el.textContent;
                }
                // Window properties (skip DOM element refs via nodeType check)
                for (const k of ['__INITIAL_SSR_DATA__', '__NEXT_DATA__',
                                  '__INITIAL_STATE__']) {
                    const v = window[k];
                    if (v && typeof v === 'object' && !v.nodeType)
                        return JSON.stringify(v);
                }
                const scripts = document.querySelectorAll(
                    'script[type="application/json"]'
                );
                for (const s of scripts) {
                    if (s.textContent && s.textContent.length > 200)
                        return s.textContent;
                }
                return null;
            }""")
            if not raw:
                return []
            data = json.loads(raw)
            return self._dig_note_items(data)
        except Exception as exc:
            logger.debug("[小红书] SSR error: %s", exc)
            return []

    def _dig_note_items(self, obj, depth=0) -> list[dict]:
        if depth > 6:
            return []
        if isinstance(obj, list):
            valid = [
                i for i in obj
                if isinstance(i, dict) and (
                    "note_card" in i or "note" in i
                    or ("id" in i and ("title" in i or "desc" in i or "user" in i))
                )
            ]
            if valid:
                return valid
            out: list[dict] = []
            for item in obj:
                if isinstance(item, dict):
                    out.extend(self._dig_note_items(item, depth + 1))
            return out
        if isinstance(obj, dict):
            for key in ("items", "notes", "feeds", "feedList", "noteList",
                        "data", "list"):
                val = obj.get(key)
                if isinstance(val, list) and val:
                    found = self._dig_note_items(val, depth + 1)
                    if found:
                        return found
            out = []
            for val in obj.values():
                if isinstance(val, (dict, list)):
                    out.extend(self._dig_note_items(val, depth + 1))
            return out
        return []

    # ── API response parsing ──

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
            or note.get("noteId", "")
            or note.get("id", "")
        )
        if not note_id or note_id in seen:
            return None
        seen.add(note_id)

        user = note.get("user") or item.get("user") or {}
        nickname = user.get("nickname", "") or user.get("name", "")
        user_id = user.get("user_id", "") or user.get("userId", "")

        title = (
            note.get("display_title", "")
            or note.get("title", "")
            or item.get("display_title", "")
        )
        desc = note.get("desc", "") or note.get("description", "")
        content = f"{title} {desc}".strip() if desc else title
        if not content and not nickname:
            return None

        link = f"https://www.xiaohongshu.com/explore/{note_id}"

        ts = (
            note.get("time", 0)
            or note.get("create_time", 0)
            or note.get("last_update_time", 0)
            or note.get("timestamp", 0)
            or item.get("time", 0)
            or item.get("timestamp", 0)
        )
        pub_date = self._ts_to_str(ts) or datetime.now().strftime("%Y-%m-%d %H:%M")

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
