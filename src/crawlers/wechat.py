"""
微信视频号爬虫 — 级联反检测策略

Data collection strategy with cascading approaches:
  S1. Standard stealth page → try 同城 tab
  S2. Persistent profile → accumulated login state
  S3. Main feed fallback → whatever is available

NOTE: WeChat Channels has the most restrictive web access and typically
requires QR-code login. This crawler may return fewer results than others.
"""
import asyncio
import json
import logging
from datetime import datetime

from .base import BaseCrawler, CrawlResult

logger = logging.getLogger(__name__)

CHANNELS_URL = "https://channels.weixin.qq.com"


class WechatCrawler(BaseCrawler):
    platform_name = "微信视频号"

    async def fetch_tongcheng(self) -> list[CrawlResult]:
        if not self.bm:
            return []

        strategies = [
            ("方案1: 隐身模式", self._strategy_stealth),
            ("方案2: 持久化浏览器", self._strategy_persistent),
            ("方案3: 标准模式兜底", self._strategy_standard),
        ]

        for name, fn in strategies:
            self._notify(f"[微信视频号] 正在尝试 {name}...")
            try:
                results = await fn()
                if results:
                    self._notify(
                        f"[微信视频号] {name} 成功！获取 {len(results)} 条")
                    return results
                self._notify(
                    f"[微信视频号] {name} 未获取到数据，尝试下一方案")
            except Exception as exc:
                logger.warning("[微信视频号] %s error: %s", name, exc)
                self._notify(
                    f"[微信视频号] {name} 出错: {exc}，尝试下一方案")

        self._notify("[微信视频号] 所有方案已尝试完毕，本轮未获取到数据")
        return []

    async def _strategy_stealth(self) -> list[CrawlResult]:
        """Strategy 1: stealth page from existing browser."""
        page = await self.bm.create_stealth_page(self.platform_name)
        try:
            return await self._do_crawl(page)
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def _strategy_persistent(self) -> list[CrawlResult]:
        """Strategy 2: persistent context maintains login state."""
        ctx = None
        try:
            ctx, page = await self.bm.create_persistent_context(
                self.platform_name, headless=True)
            return await self._do_crawl(page)
        finally:
            if ctx:
                try:
                    await ctx.close()
                except Exception:
                    pass

    async def _strategy_standard(self) -> list[CrawlResult]:
        """Strategy 3: standard page (existing get_page)."""
        try:
            page = await self.bm.get_page(self.platform_name)
        except Exception as exc:
            logger.warning("[微信视频号] Browser not available: %s", exc)
            return []
        return await self._do_crawl(page, save_cookies=True)

    async def _do_crawl(self, page, save_cookies=False) -> list[CrawlResult]:
        """Core crawl logic shared by all strategies."""
        captured: list = []

        async def on_response(response):
            url = response.url
            if response.status != 200:
                return
            hit = (
                "cgi-bin/mmfinderassistant" in url
                or "finder" in url
                or "feedlist" in url
                or "/channels/" in url
                or "mmfinder" in url
            )
            if not hit:
                return
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct or "javascript" in ct:
                    body = await response.json()
                    captured.append(body)
                    logger.debug("[微信视频号] API captured: %s", url[:150])
            except Exception:
                pass

        page.on("response", on_response)
        ssr_items: list = []

        try:
            await page.goto(
                CHANNELS_URL, wait_until="domcontentloaded", timeout=30_000,
            )
            await asyncio.sleep(4)

            title = await page.title()
            logger.info("[微信视频号] Page: '%s' @ %s", title, page.url)

            ssr_items = await self._extract_ssr(page)
            if ssr_items:
                logger.info("[微信视频号] SSR: found %d items", len(ssr_items))

            found_local = False
            for label in ("同城", "附近", "本地"):
                selectors = [
                    f'a:has-text("{label}")',
                    f'div[role="tab"]:has-text("{label}")',
                    f'span:has-text("{label}")',
                    f'text={label}',
                ]
                for sel in selectors:
                    try:
                        tab = page.locator(sel).first
                        if await tab.is_visible(timeout=2000):
                            await tab.click()
                            await asyncio.sleep(3)
                            logger.info(
                                "[微信视频号] Clicked '%s' tab via %s",
                                label, sel)
                            found_local = True
                            break
                    except Exception:
                        continue
                if found_local:
                    break

            if not found_local:
                try:
                    clicked = await page.evaluate("""() => {
                        const els = document.querySelectorAll(
                            'a, div, span, li');
                        for (const el of els) {
                            const t = el.textContent?.trim();
                            if (t === '同城' || t === '附近'
                                || t === '本地') {
                                el.click();
                                return t;
                            }
                        }
                        return null;
                    }""")
                    if clicked:
                        await asyncio.sleep(3)
                        logger.info("[微信视频号] Clicked '%s' via JS", clicked)
                    else:
                        logger.info(
                            "[微信视频号] 同城 tab not found, using default")
                except Exception:
                    logger.info(
                        "[微信视频号] 同城 tab not found, using default")

            for _ in range(5):
                await page.evaluate(
                    "window.scrollTo(0, document.body.scrollHeight)"
                )
                await asyncio.sleep(2)
            await asyncio.sleep(1)

        except Exception as exc:
            logger.warning("[微信视频号] Page error: %s", exc)
        finally:
            page.remove_listener("response", on_response)

        if save_cookies:
            await self.bm.save_cookies(self.platform_name)

        results: list = []
        seen: set = set()

        for item in ssr_items:
            r = self._parse_item(item, seen)
            if r:
                results.append(r)

        for body in captured:
            results.extend(self._parse_response(body, seen))

        logger.info(
            "[微信视频号] Total: %d items (SSR=%d, API captures=%d)",
            len(results), len(ssr_items), len(captured),
        )
        return results

    # ── SSR extraction ──

    async def _extract_ssr(self, page) -> list:
        try:
            raw = await page.evaluate("""() => {
                for (const id of ['__NEXT_DATA__', 'RENDER_DATA',
                                   '__INITIAL_DATA__']) {
                    const el = document.getElementById(id);
                    if (el && el.textContent && el.textContent.length > 10)
                        return el.textContent;
                }
                for (const k of ['__INITIAL_DATA__', '__NEXT_DATA__']) {
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
            return self._dig_feed_items(data)
        except Exception as exc:
            logger.debug("[微信视频号] SSR error: %s", exc)
            return []

    def _dig_feed_items(self, obj, depth=0) -> list:
        if depth > 6:
            return []
        if isinstance(obj, list):
            valid = [
                i for i in obj
                if isinstance(i, dict) and (
                    "objectId" in i or "feedId" in i or "id" in i
                ) and (
                    "nickname" in i or "description" in i
                    or "object" in i or "author" in i
                )
            ]
            if valid:
                return valid
            out: list = []
            for item in obj:
                if isinstance(item, dict):
                    out.extend(self._dig_feed_items(item, depth + 1))
            return out
        if isinstance(obj, dict):
            for key in ("feedList", "objectList", "list", "data", "items"):
                val = obj.get(key)
                if isinstance(val, list) and val:
                    found = self._dig_feed_items(val, depth + 1)
                    if found:
                        return found
            out = []
            for val in obj.values():
                if isinstance(val, (dict, list)):
                    out.extend(self._dig_feed_items(val, depth + 1))
            return out
        return []

    # ── API response parsing ──

    def _parse_response(self, body: dict, seen: set) -> list:
        items = []
        data = body.get("data", {})
        feed_list = (
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

    def _parse_item(self, item: dict, seen: set):
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
        if not content and not nickname:
            return None

        link = item.get("shareUrl", "") or item.get("url", "") or ""

        ts = (
            obj.get("createTime", 0)
            or item.get("createTime", 0)
            or obj.get("create_time", 0)
            or obj.get("timestamp", 0)
        )
        pub_date = (self._ts_to_str(ts)
                    or datetime.now().strftime("%Y-%m-%d %H:%M"))

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
