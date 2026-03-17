"""
抖音爬虫 — Playwright SSR extraction + network interception

Data collection strategy:
  1. Navigate to douyin.com with saved cookies
  2. Try to switch to 同城 (local city) tab
  3. Extract feed data embedded in SSR HTML (RENDER_DATA)
  4. Intercept /aweme/ API responses triggered by scrolling
  5. Combine both sources and deduplicate
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

    async def _try_switch_tongcheng(self, page) -> bool:
        """Try multiple selectors to switch to 同城 tab."""
        selectors = [
            'a:has-text("同城")',
            'div[role="tab"]:has-text("同城")',
            'span:has-text("同城")',
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

        try:
            found = await page.evaluate("""() => {
                const links = document.querySelectorAll('a, div, span, li');
                for (const el of links) {
                    const t = el.textContent?.trim();
                    if (t === '同城' || t === '附近') {
                        el.click();
                        return t;
                    }
                }
                return null;
            }""")
            if found:
                await asyncio.sleep(3)
                logger.info("[抖音] Clicked '%s' tab via JS", found)
                return True
        except Exception:
            pass

        logger.info("[抖音] 同城 tab not found on desktop web, using default feed")
        return False

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

        try:
            await page.goto(
                DOUYIN_URL, wait_until="domcontentloaded", timeout=30_000,
            )
            await asyncio.sleep(4)

            title = await page.title()
            logger.info("[抖音] Page: '%s' @ %s", title, page.url)

            await self._try_switch_tongcheng(page)
            await asyncio.sleep(2)

            ssr_items = await self._extract_ssr(page)
            if ssr_items:
                logger.info("[抖音] SSR: found %d items in page HTML", len(ssr_items))
            else:
                logger.info("[抖音] SSR: no embedded data found, relying on API interception")

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

        for item in ssr_items:
            r = self._parse_aweme(item, seen)
            if r:
                results.append(r)

        for body in captured:
            results.extend(self._parse_response(body, seen))

        logger.info(
            "[抖音] Total: %d items (SSR=%d, API captures=%d)",
            len(results), len(ssr_items), len(captured),
        )
        return results

    # ── SSR extraction ──

    async def _extract_ssr(self, page) -> list[dict]:
        """Pull video items from Douyin's server-rendered HTML data."""
        try:
            raw = await page.evaluate("""() => {
                const out = [];
                const seen = new Set();
                // Collect all script[type=application/json] by element
                document.querySelectorAll('script[type="application/json"]').forEach(s => {
                    if (s.textContent && s.textContent.length > 10) {
                        out.push({t: s.id || 'script', d: s.textContent});
                        if (s.id) seen.add(s.id);
                    }
                });
                // Window properties (skip DOM element refs)
                for (const k of ['__NEXT_DATA__', '__INITIAL_STATE__',
                                  '__INITIAL_SSR_DATA__']) {
                    if (seen.has(k)) continue;
                    const v = window[k];
                    if (v && typeof v === 'object' && !v.nodeType) {
                        out.push({t: k, d: JSON.stringify(v)});
                    }
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
            logger.debug("[抖音] SSR extraction error: %s", exc)
            return []

    def _dig_aweme_list(self, obj, depth=0) -> list[dict]:
        """Recursively find arrays of aweme-like objects."""
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
                        "feedList", "recommendList"):
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
