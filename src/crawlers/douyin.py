"""
抖音爬虫 — 级联反检测策略 + 同城搜索

When target_city is configured, tries 5 anti-detection strategies
in cascade to search for city-specific content on Douyin:

  Strategy 1: Standard browser + playwright-stealth → search page
  Strategy 2: Persistent browser profile + stealth → search page
  Strategy 3: Chrome extension capture (headed) → search page
  Strategy 4: User's real Chrome via CDP → search page
  Strategy 5: Android emulator + Douyin mobile app (Appium)

If all search strategies fail (e.g. CAPTCHA), falls back to main feed
which always returns 推荐/精选 content.

Without target_city, goes directly to main feed.
"""
import asyncio
import json
import logging
from datetime import datetime
from urllib.parse import quote, unquote

from .base import BaseCrawler, CrawlResult
from .browser_manager import human_scroll, human_delay, warm_up_page

logger = logging.getLogger(__name__)

DOUYIN_URL = "https://www.douyin.com"

CAPTCHA_INDICATORS_TITLE = ("验证", "captcha", "verify", "安全检测", "人机")
CAPTCHA_INDICATORS_URL = ("verify", "captcha", "slide", "sso/login")
CAPTCHA_SELECTORS = (
    '[class*="captcha"]',
    '[class*="verify"]',
    '[id*="captcha"]',
    '[class*="slide-bar"]',
    '[class*="secsdk"]',
    '[class*="captcha_container"]',
)


class DouyinCrawler(BaseCrawler):
    platform_name = "抖音"

    async def fetch_tongcheng(self) -> list[CrawlResult]:
        if not self.bm:
            return []

        city = self.target_city
        if not city:
            self._notify("[抖音] 未设置目标城市，使用主页信息流采集")
            return await self._strategy_main_feed()

        search_url = (
            f"https://www.douyin.com/search/{quote(city)}"
            f"?type=video"
        )

        strategies = [
            ("方案1: 隐身模式搜索",
             lambda: self._strategy_stealth_search(search_url)),
            ("方案2: 持久化浏览器搜索",
             lambda: self._strategy_persistent_search(search_url)),
            ("方案3: 扩展程序增强捕获",
             lambda: self._strategy_extension_search(search_url)),
            ("方案4: 用户Chrome浏览器",
             lambda: self._strategy_cdp_search(search_url)),
            ("方案5: Android模拟器(Appium)",
             lambda: self._strategy_emulator(city)),
        ]

        for name, fn in strategies:
            self._notify(f"[抖音] 正在尝试 {name}...")
            try:
                results = await fn()
                if results:
                    self._notify(
                        f"[抖音] {name} 成功！获取 {len(results)} 条同城数据")
                    return results
                self._notify(f"[抖音] {name} 未获取到数据，准备尝试下一方案")
            except Exception as exc:
                logger.warning("[抖音] %s error: %s", name, exc)
                self._notify(
                    f"[抖音] {name} 出错: {exc}，准备尝试下一方案")

        self._notify("[抖音] 所有搜索方案均未成功，使用主页信息流兜底采集")
        return await self._strategy_main_feed()

    # ═══════════════════════════════════════════════════
    #  CAPTCHA detection
    # ═══════════════════════════════════════════════════

    async def _detect_captcha(self, page) -> bool:
        """Check if the current page shows a CAPTCHA challenge."""
        try:
            title = (await page.title()).lower()
            url = page.url.lower()

            if any(k in title for k in CAPTCHA_INDICATORS_TITLE):
                logger.info("[抖音] CAPTCHA detected in title: '%s'", title)
                return True
            if any(k in url for k in CAPTCHA_INDICATORS_URL):
                logger.info("[抖音] CAPTCHA detected in URL: %s", url)
                return True

            sel = ", ".join(CAPTCHA_SELECTORS)
            cap = await page.query_selector(sel)
            if cap:
                logger.info("[抖音] CAPTCHA element found on page")
                return True

            return False
        except Exception:
            return False

    # ═══════════════════════════════════════════════════
    #  Shared search + capture logic
    # ═══════════════════════════════════════════════════

    async def _do_search_and_capture(self, page, search_url,
                                      wait_for_captcha=False
                                      ) -> list[CrawlResult]:
        """Navigate to search URL, intercept APIs, parse results.

        If wait_for_captcha=True and CAPTCHA is detected, waits up to 60s
        for the user to solve it manually in the headed browser.
        """
        captured = []

        async def on_response(response):
            if response.status != 200:
                return
            url = response.url
            if "/aweme/" not in url and "/search/" not in url:
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
            await warm_up_page(page, search_url)

            await page.goto(
                search_url, wait_until="domcontentloaded", timeout=30_000)
            await human_delay(3.0, 5.0)

            if await self._detect_captcha(page):
                if wait_for_captcha:
                    self._notify(
                        "[抖音] 检测到验证码，请在弹出的浏览器中手动完成验证...")
                    for _ in range(30):
                        await asyncio.sleep(2)
                        if not await self._detect_captcha(page):
                            self._notify("[抖音] 验证已通过！正在采集数据...")
                            break
                    else:
                        self._notify("[抖音] 验证码超时，跳过此方案")
                        return []
                else:
                    return []

            title = await page.title()
            logger.info("[抖音] Search page: '%s' @ %s", title, page.url)

            ssr_items = await self._extract_ssr(page)
            if ssr_items:
                logger.info("[抖音] SSR: %d items from search", len(ssr_items))

            await human_scroll(page, times=6, jitter=True)
            await human_delay(1.0, 2.0)

        except Exception as exc:
            logger.warning("[抖音] Search page error: %s", exc)
            return []
        finally:
            page.remove_listener("response", on_response)

        results = []
        seen: set = set()

        for item in ssr_items:
            r = self._parse_aweme(item, seen)
            if r:
                results.append(r)

        for body in captured:
            results.extend(self._parse_any_response(body, seen))

        return results

    # ═══════════════════════════════════════════════════
    #  Strategy 1: Stealth search
    # ═══════════════════════════════════════════════════

    async def _strategy_stealth_search(self, search_url) -> list[CrawlResult]:
        """Standard browser + playwright-stealth patches → search page."""
        page = await self.bm.create_stealth_page(self.platform_name)
        try:
            return await self._do_search_and_capture(page, search_url)
        finally:
            try:
                await page.close()
            except Exception:
                pass

    # ═══════════════════════════════════════════════════
    #  Strategy 2: Persistent profile search
    # ═══════════════════════════════════════════════════

    async def _strategy_persistent_search(self, search_url
                                           ) -> list[CrawlResult]:
        """Persistent browser context + stealth → search page."""
        ctx = None
        try:
            ctx, page = await self.bm.create_persistent_context(
                self.platform_name, headless=True)
            return await self._do_search_and_capture(page, search_url)
        finally:
            if ctx:
                try:
                    await ctx.close()
                except Exception:
                    pass

    # ═══════════════════════════════════════════════════
    #  Strategy 3: Chrome extension capture (headed)
    # ═══════════════════════════════════════════════════

    async def _strategy_extension_search(self, search_url
                                          ) -> list[CrawlResult]:
        """Persistent context + Chrome extension (headed mode).

        The extension injects fetch/XHR patches at the page level
        to capture API responses. Since it runs headed, the user
        can manually solve CAPTCHA if it appears.
        """
        ext_path = str(self.bm.get_extension_path())
        ctx = None
        try:
            ctx, page = await self.bm.create_persistent_context(
                self.platform_name, headless=False,
                extension_path=ext_path)

            results = await self._do_search_and_capture(
                page, search_url, wait_for_captcha=True)

            if results:
                return results

            try:
                ext_data = await page.evaluate(
                    "window.__CRAWLER_CAPTURED__ || []")
                if ext_data:
                    logger.info(
                        "[抖音] Extension captured %d responses",
                        len(ext_data))
                    seen: set = set()
                    for entry in ext_data:
                        body = entry.get("data", {})
                        if isinstance(body, dict):
                            results.extend(
                                self._parse_any_response(body, seen))
            except Exception as exc:
                logger.debug("[抖音] Extension data read error: %s", exc)

            return results
        finally:
            if ctx:
                try:
                    await ctx.close()
                except Exception:
                    pass

    # ═══════════════════════════════════════════════════
    #  Strategy 4: CDP with user's real Chrome
    # ═══════════════════════════════════════════════════

    async def _strategy_cdp_search(self, search_url) -> list[CrawlResult]:
        """Launch user's real Chrome with their actual profile via CDP.

        This is the most "human" approach — uses the real browser with
        all the user's history, cookies, and extensions. Headed mode
        allows manual CAPTCHA solving.
        """
        browser = None
        proc = None
        try:
            browser, page, proc = await self.bm.create_cdp_user_page(
                self.platform_name)
            return await self._do_search_and_capture(
                page, search_url, wait_for_captcha=True)
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass

    # ═══════════════════════════════════════════════════
    #  Strategy 5: Android emulator + Douyin mobile app
    # ═══════════════════════════════════════════════════

    async def _strategy_emulator(self, city: str) -> list[CrawlResult]:
        """Automate the real Douyin mobile app via Appium + Android emulator.

        The mobile app has a native 同城 tab (unlike the web version).
        Requires: Android emulator + Appium server + Douyin APK installed.
        Gracefully returns [] if Appium is not available.
        """
        from . import appium_douyin

        if not appium_douyin.is_available():
            self._notify(
                "[抖音] 方案5: Appium 未安装，跳过模拟器方案"
                "（需 pip install Appium-Python-Client）")
            return []

        raw_items = await appium_douyin.fetch_douyin_tongcheng(
            city, status_callback=self.status_callback)

        results = []
        for item in raw_items:
            results.append(CrawlResult(
                platform="抖音",
                item_id=item.get("item_id", ""),
                nickname=item.get("nickname", ""),
                content=item.get("content", ""),
                link=item.get("link", ""),
                publish_date=item.get("publish_date", ""),
            ))
        return results

    # ═══════════════════════════════════════════════════
    #  Strategy 6: Main feed fallback (always works)
    # ═══════════════════════════════════════════════════

    async def _strategy_main_feed(self) -> list[CrawlResult]:
        """Navigate to main Douyin page, collect whatever feed is shown."""
        try:
            page = await self.bm.get_page(self.platform_name)
        except Exception as exc:
            logger.warning("[抖音] Browser not available: %s", exc)
            return []

        captured: list = []

        async def on_response(response):
            if response.status != 200:
                return
            url = response.url
            if "/aweme/" not in url:
                return
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct or "javascript" in ct:
                    body = await response.json()
                    captured.append(body)
                    logger.debug("[抖音] Feed captured: %s",
                                 url.split("?")[0][-60:])
            except Exception:
                pass

        page.on("response", on_response)

        try:
            await page.goto(
                DOUYIN_URL, wait_until="domcontentloaded", timeout=30_000)
            await human_delay(4.0, 6.0)

            title = await page.title()
            logger.info("[抖音] Feed page: '%s' @ %s", title, page.url)

            ssr_items = await self._extract_ssr(page)
            if ssr_items:
                logger.info("[抖音] SSR: %d items", len(ssr_items))

            await human_scroll(page, times=6, jitter=True)
            await human_delay(1.0, 2.0)

        except Exception as exc:
            logger.warning("[抖音] Feed page error: %s", exc)
        finally:
            page.remove_listener("response", on_response)

        await self.bm.save_cookies(self.platform_name)

        results: list[CrawlResult] = []
        seen: set = set()

        for item in ssr_items:
            r = self._parse_aweme(item, seen)
            if r:
                results.append(r)

        for body in captured:
            results.extend(self._parse_any_response(body, seen))

        logger.info("[抖音] Feed total: %d items from %d API responses",
                    len(results), len(captured))
        return results

    # ═══════════════════════════════════════════════════
    #  SSR extraction
    # ═══════════════════════════════════════════════════

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
            all_items: list = []
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
        items: list = []
        for aweme in self._extract_awemes(body):
            r = self._parse_aweme(aweme, seen)
            if r:
                items.append(r)
        return items

    def _extract_awemes(self, obj, depth=0) -> list:
        """Recursively extract aweme objects from any response format."""
        if depth > 8:
            return []

        if isinstance(obj, list):
            out = []
            for item in obj:
                if not isinstance(item, dict):
                    continue
                ai = item.get("aweme_info")
                if isinstance(ai, dict) and (
                    "aweme_id" in ai or "desc" in ai
                ):
                    out.append(ai)
                    continue
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
