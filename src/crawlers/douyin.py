"""
抖音爬虫 — 城市搜索 + 同城 API + 信息流

Strategy (when target_city is configured):
  1. Search "{city} 同城" on douyin.com → city-relevant content
  2. Call /aweme/v1/web/nearby/feed/ API with cookies
  3. Extract SSR data + intercept scroll API responses

Without target_city:
  1. Try clicking 同城 tab (unlikely on desktop web)
  2. Try nearby API
  3. Fall back to default feed

The 同城 tab is a mobile-app feature; desktop web doesn't show it.
City-based search is the most reliable way to get local content.
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
            if "/aweme/" not in url and "/nearby/" not in url:
                return
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct or "javascript" in ct:
                    body = await response.json()
                    captured.append(body)
                    logger.debug("[抖音] API captured: %s", url[:150])
            except Exception:
                pass

        page.on("response", on_response)
        search_items: list[dict] = []
        nearby_items: list[dict] = []
        ssr_items: list[dict] = []

        try:
            city = self.target_city

            if city:
                # ── Primary: city-based search ──
                search_items = await self._search_city(page, city)
                logger.info("[抖音] City search '%s': %d items",
                            city, len(search_items))
            else:
                # No city configured — navigate to main page
                await page.goto(
                    DOUYIN_URL, wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(5)

            # ── Try nearby API (works with or without city) ──
            if len(search_items) < 10:
                nearby_items = await self._fetch_nearby_api(page)
                if nearby_items:
                    logger.info("[抖音] Nearby API: %d items",
                                len(nearby_items))

            # ── SSR extraction from current page ──
            ssr_items = await self._extract_ssr(page)
            if ssr_items:
                logger.info("[抖音] SSR: %d items", len(ssr_items))

            # ── Scroll to trigger more API responses ──
            for _ in range(5):
                await page.evaluate(
                    "window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(2)
            await asyncio.sleep(1)

        except Exception as exc:
            logger.warning("[抖音] Page error: %s", exc)
        finally:
            page.remove_listener("response", on_response)

        await self.bm.save_cookies(self.platform_name)

        results: list[CrawlResult] = []
        seen: set[str] = set()

        for item in search_items:
            r = self._parse_aweme(item, seen)
            if r:
                results.append(r)
        search_count = len(results)

        for item in nearby_items:
            r = self._parse_aweme(item, seen)
            if r:
                results.append(r)

        for item in ssr_items:
            r = self._parse_aweme(item, seen)
            if r:
                results.append(r)

        for body in captured:
            results.extend(self._parse_response(body, seen))

        logger.info(
            "[抖音] Total: %d (search=%d, nearby=%d, SSR=%d, captured=%d)",
            len(results), search_count, len(nearby_items),
            len(ssr_items), len(captured),
        )
        return results

    # ── Strategy: city-based search ──

    async def _search_city(self, page, city: str) -> list[dict]:
        """Navigate to Douyin search and search for city content."""
        all_items: list[dict] = []

        queries = [f"{city}", f"{city} 同城"]
        for query in queries:
            try:
                search_url = (
                    f"{DOUYIN_URL}/search/{quote(query)}"
                    f"?type=video"
                )
                await page.goto(
                    search_url, wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(4)

                title = await page.title()
                logger.info("[抖音] Search page: '%s' @ %s", title, page.url)

                ssr = await self._extract_ssr(page)
                if ssr:
                    all_items.extend(ssr)
                    logger.info("[抖音] Search '%s' SSR: %d items",
                                query, len(ssr))

                for _ in range(3):
                    await page.evaluate(
                        "window.scrollTo(0, document.body.scrollHeight)")
                    await asyncio.sleep(2)

                if all_items:
                    break
            except Exception as exc:
                logger.debug("[抖音] Search '%s' error: %s", query, exc)

        # Also try the web search API directly
        if len(all_items) < 5:
            for query in queries:
                try:
                    api_items = await self._search_api(page, query)
                    if api_items:
                        all_items.extend(api_items)
                        logger.info("[抖音] Search API '%s': %d items",
                                    query, len(api_items))
                        break
                except Exception as exc:
                    logger.debug("[抖音] Search API '%s' error: %s",
                                 query, exc)

        return all_items

    async def _search_api(self, page, keyword: str) -> list[dict]:
        """Call the Douyin web search API from browser context."""
        encoded = quote(keyword)
        data = await page.evaluate("""async (kw) => {
            try {
                const r = await fetch(
                    '/aweme/v1/web/general/search/single/'
                    + '?keyword=' + kw
                    + '&count=20&search_source=normal_search'
                    + '&query_correct_type=1&is_filter_search=0'
                    + '&offset=0&search_id=',
                    {credentials: 'include',
                     headers: {'Accept': 'application/json'}}
                );
                if (!r.ok) return null;
                return await r.json();
            } catch { return null; }
        }""", encoded)
        if data and isinstance(data, dict):
            return self._dig_aweme_list(data)
        return []

    # ── Strategy: nearby API ──

    async def _fetch_nearby_api(self, page) -> list[dict]:
        """Call Douyin's web nearby feed API directly."""
        endpoints = [
            "/aweme/v1/web/nearby/feed/?count=30&"
            "aid=6383&channel=channel_nearby&offset=0",
        ]
        for ep in endpoints:
            try:
                data = await page.evaluate("""async (url) => {
                    try {
                        const r = await fetch(url, {
                            credentials: 'include',
                            headers: {'Accept': 'application/json'}
                        });
                        if (!r.ok) return null;
                        return await r.json();
                    } catch { return null; }
                }""", ep)
                if data and isinstance(data, dict):
                    items = self._dig_aweme_list(data)
                    if items:
                        return items
            except Exception as exc:
                logger.debug("[抖音] Nearby API error: %s", exc)
        return []

    # ── SSR extraction ──

    async def _extract_ssr(self, page) -> list[dict]:
        try:
            raw = await page.evaluate("""() => {
                const out = [];
                const seen = new Set();
                document.querySelectorAll(
                    'script[type="application/json"]'
                ).forEach(s => {
                    if (s.textContent && s.textContent.length > 10) {
                        out.push({t: s.id || 'script', d: s.textContent});
                        if (s.id) seen.add(s.id);
                    }
                });
                for (const k of ['__NEXT_DATA__', '__INITIAL_STATE__',
                                  '__INITIAL_SSR_DATA__']) {
                    if (seen.has(k)) continue;
                    const v = window[k];
                    if (v && typeof v === 'object' && !v.nodeType)
                        out.push({t: k, d: JSON.stringify(v)});
                }
                return out;
            }""")
            if not raw:
                return []
            all_items: list[dict] = []
            for entry in raw:
                text = entry.get("d", "")
                try:
                    decoded = unquote(text)
                    data = json.loads(decoded)
                except Exception:
                    try:
                        data = json.loads(text)
                    except Exception:
                        continue
                all_items.extend(self._dig_aweme_list(data))
            return all_items
        except Exception as exc:
            logger.debug("[抖音] SSR error: %s", exc)
            return []

    def _dig_aweme_list(self, obj, depth=0) -> list[dict]:
        if depth > 6:
            return []
        if isinstance(obj, list):
            valid = [
                item for item in obj
                if isinstance(item, dict) and (
                    "aweme_id" in item or "awemeId" in item
                    or ("desc" in item and "author" in item)
                )
            ]
            if valid:
                return valid
            out: list[dict] = []
            for item in obj:
                if isinstance(item, dict):
                    out.extend(self._dig_aweme_list(item, depth + 1))
            return out
        if isinstance(obj, dict):
            for key in ("awemeList", "aweme_list", "videoList", "video_list",
                        "feedList", "recommendList", "data"):
                val = obj.get(key)
                if isinstance(val, list) and val:
                    found = self._dig_aweme_list(val, depth + 1)
                    if found:
                        return found
            out = []
            for val in obj.values():
                if isinstance(val, (dict, list)):
                    out.extend(self._dig_aweme_list(val, depth + 1))
            return out
        return []

    # ── Response parsing ──

    def _parse_response(self, body: dict, seen: set) -> list[CrawlResult]:
        items: list[CrawlResult] = []
        aweme_list = body.get("aweme_list") or []
        if not isinstance(aweme_list, list):
            nested = body.get("data")
            if isinstance(nested, dict):
                aweme_list = (
                    nested.get("aweme_list")
                    or nested.get("data")
                    or nested.get("list")
                    or []
                )
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
