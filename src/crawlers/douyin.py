"""
抖音爬虫 — 主信息流 + 网络拦截

Strategy:
  1. Navigate to douyin.com (main feed — always works)
  2. Intercept all /aweme/ API responses during page load + scroll
  3. Parse feed items from intercepted responses

NOTE: Douyin desktop web does not support city-based content (同城).
The search page is blocked by CAPTCHA for automated browsers, and
the search API requires security tokens (X-Bogus) that cannot be
generated outside Douyin's own JS. The main feed returns 推荐/精选
content based on the logged-in user's IP location and preferences.
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
            if "/aweme/" not in url:
                return
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct or "javascript" in ct:
                    body = await response.json()
                    captured.append(body)
                    logger.debug("[抖音] Captured: %s",
                                 url.split("?")[0][-60:])
            except Exception:
                pass

        page.on("response", on_response)

        try:
            # Always navigate to main page (search triggers CAPTCHA)
            await page.goto(
                DOUYIN_URL, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(5)

            title = await page.title()
            logger.info("[抖音] Page: '%s' @ %s", title, page.url)

            # Also extract SSR data from initial page load
            ssr_items = await self._extract_ssr(page)
            if ssr_items:
                logger.info("[抖音] SSR: %d items", len(ssr_items))

            # Scroll to trigger more feed API calls
            for _ in range(6):
                await page.evaluate(
                    "window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(2.5)
            await asyncio.sleep(1)

        except Exception as exc:
            logger.warning("[抖音] Page error: %s", exc)
        finally:
            page.remove_listener("response", on_response)

        await self.bm.save_cookies(self.platform_name)

        # Parse all captured API responses
        results: list[CrawlResult] = []
        seen: set[str] = set()

        # SSR items first
        for item in (ssr_items if 'ssr_items' in dir() else []):
            r = self._parse_aweme(item, seen)
            if r:
                results.append(r)

        # Network-intercepted API responses
        for body in captured:
            parsed = self._parse_any_response(body, seen)
            results.extend(parsed)

        logger.info("[抖音] Total: %d items from %d API responses",
                    len(results), len(captured))
        return results

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
                all_items.extend(self._extract_awemes(data))
            return all_items
        except Exception as exc:
            logger.debug("[抖音] SSR error: %s", exc)
            return []

    # ── Universal response parser ──

    def _parse_any_response(self, body: dict, seen: set) -> list[CrawlResult]:
        if not isinstance(body, dict):
            return []
        items: list[CrawlResult] = []
        for aweme in self._extract_awemes(body):
            r = self._parse_aweme(aweme, seen)
            if r:
                items.append(r)
        return items

    def _extract_awemes(self, obj, depth=0) -> list[dict]:
        """Recursively extract aweme objects from any response format."""
        if depth > 8:
            return []

        if isinstance(obj, list):
            out: list[dict] = []
            for item in obj:
                if not isinstance(item, dict):
                    continue
                # Search result wrapper
                ai = item.get("aweme_info")
                if isinstance(ai, dict) and (
                    "aweme_id" in ai or "desc" in ai
                ):
                    out.append(ai)
                    continue
                # Direct aweme
                if "aweme_id" in item or "awemeId" in item:
                    out.append(item)
                    continue
                if "desc" in item and ("author" in item or "nickname" in item):
                    out.append(item)
                    continue
                out.extend(self._extract_awemes(item, depth + 1))
            return out

        if isinstance(obj, dict):
            for key in ("aweme_list", "awemeList", "data", "list",
                        "videoList", "video_list", "feedList",
                        "recommendList"):
                val = obj.get(key)
                if isinstance(val, list) and val:
                    found = self._extract_awemes(val, depth + 1)
                    if found:
                        return found
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
