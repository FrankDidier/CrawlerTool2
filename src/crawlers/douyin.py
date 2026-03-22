"""
抖音爬虫 — 多策略同城内容采集

Strategy order:
  1. Navigate to douyin.com, try to click 同城 tab (if visible)
  2. Call /aweme/v1/web/nearby/feed/ API directly from browser context
  3. Navigate to /discover page and look for local content
  4. Fall back to default feed with network interception

The 同城 tab is primarily a *mobile app* feature; the desktop web
often does not show it.  Strategy #2 (direct API call) is the most
reliable way to get local-city content from the web.
"""
import asyncio
import json
import logging
from datetime import datetime
from urllib.parse import unquote

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
        ssr_items: list[dict] = []
        nearby_items: list[dict] = []

        try:
            await page.goto(
                DOUYIN_URL, wait_until="domcontentloaded", timeout=30_000,
            )
            await asyncio.sleep(5)

            title = await page.title()
            logger.info("[抖音] Page: '%s' @ %s", title, page.url)

            # Strategy 1: Try clicking 同城 tab
            clicked = await self._try_switch_tongcheng(page)

            if not clicked:
                # Strategy 2: Direct API call for nearby feed
                nearby_items = await self._fetch_nearby_api(page)
                if nearby_items:
                    logger.info("[抖音] 同城 API: got %d items",
                                len(nearby_items))

            # SSR extraction (after potential tab switch)
            await asyncio.sleep(2)
            ssr_items = await self._extract_ssr(page)
            if ssr_items:
                logger.info("[抖音] SSR: found %d items", len(ssr_items))

            # Scroll to trigger API responses
            for _ in range(5):
                await page.evaluate(
                    "window.scrollTo(0, document.body.scrollHeight)"
                )
                await asyncio.sleep(2)
            await asyncio.sleep(1)

        except Exception as exc:
            logger.warning("[抖音] Page error: %s", exc)
        finally:
            page.remove_listener("response", on_response)

        await self.bm.save_cookies(self.platform_name)

        results: list[CrawlResult] = []
        seen: set[str] = set()

        for item in nearby_items:
            r = self._parse_aweme(item, seen)
            if r:
                results.append(r)

        nearby_count = len(results)

        for item in ssr_items:
            r = self._parse_aweme(item, seen)
            if r:
                results.append(r)

        for body in captured:
            results.extend(self._parse_response(body, seen))

        logger.info(
            "[抖音] Total: %d items (nearby_api=%d, SSR=%d, captures=%d)",
            len(results), nearby_count, len(ssr_items), len(captured),
        )
        return results

    # ── Strategy 1: Tab click ──

    async def _try_switch_tongcheng(self, page) -> bool:
        """Try to find and click 同城 tab in the web UI."""
        selectors = [
            '[data-e2e="channel-item"]:has-text("同城")',
            '[data-e2e="channel-item"]:has-text("附近")',
            'a:has-text("同城")',
            'div[role="tab"]:has-text("同城")',
            'span:has-text("同城")',
            'a:has-text("附近")',
            'span:has-text("附近")',
            'text=同城',
        ]
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=2000):
                    await el.click()
                    await asyncio.sleep(3)
                    logger.info("[抖音] Clicked 同城 tab via: %s", sel)
                    return True
            except Exception:
                continue

        found = await page.evaluate("""() => {
            const all = document.querySelectorAll(
                'a, div, span, li, [role="tab"]'
            );
            for (const el of all) {
                const t = (el.textContent || '').trim();
                if (t === '同城' || t === '附近' || t === '本地') {
                    el.click();
                    return t;
                }
            }
            return null;
        }""")
        if found:
            await asyncio.sleep(3)
            logger.info("[抖音] Clicked '%s' via JS scan", found)
            return True

        logger.info("[抖音] 同城 tab not found in desktop web UI")
        return False

    # ── Strategy 2: Direct nearby API call ──

    async def _fetch_nearby_api(self, page) -> list[dict]:
        """Call Douyin's web nearby feed API directly from browser context.

        This bypasses the need for a visible tab — it calls the API
        endpoint that the 同城 tab would call, using the logged-in
        user's cookies for authentication and city detection.
        """
        endpoints = [
            "/aweme/v1/web/nearby/feed/?count=30&"
            "aid=6383&channel=channel_nearby&offset=0",
            "/aweme/v1/web/general/search/single/?"
            "keyword=同城&count=20&search_source=normal_search",
        ]

        all_items: list[dict] = []
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
                        logger.info("[抖音] nearby API (%s): %d items",
                                    ep.split("?")[0], len(items))
                        all_items.extend(items)
                        break
            except Exception as exc:
                logger.debug("[抖音] nearby API failed (%s): %s",
                             ep.split("?")[0], exc)
        return all_items

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

    # ── API response parsing ──

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
