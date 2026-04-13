"""
抖音爬虫 — 地理位置伪装采集同城内容

通过 Playwright 地理位置伪装（GPS spoofing），让抖音根据目标城市
坐标推送附近/同城内容，而非关键词搜索结果。

策略级联：
  方案1: 桌面浏览器 + 地理位置伪装（无头，速度快）
  方案2: 移动端模拟 + 地理位置伪装（有头，支持手动过验证码）
  兜底:  主页信息流（无城市过滤）

会话缓存：首次成功后保留浏览器会话，后续采集周期直接复用，
避免重复登录和验证码。方案2 过完验证码后会保存 Cookie，下一
周期方案1 即可凭 Cookie 免验证采集。
"""
import asyncio
import json
import logging
from datetime import datetime
from urllib.parse import unquote

from .base import BaseCrawler, CrawlResult
from .browser_manager import human_scroll, human_delay, apply_stealth

logger = logging.getLogger(__name__)

DOUYIN_URL = "https://www.douyin.com"

CAPTCHA_INDICATORS_TITLE = ("验证", "captcha", "verify", "安全检测", "人机")
CAPTCHA_INDICATORS_URL = ("verify", "captcha", "slide", "sso/login")
CAPTCHA_SELECTORS = (
    '[class*="captcha"]', '[class*="verify"]', '[id*="captcha"]',
    '[class*="slide-bar"]', '[class*="secsdk"]',
    '[class*="captcha_container"]',
)

FEED_API_PATTERNS = (
    "/aweme/", "/feed/", "/nearby/", "/same_city/",
    "/recommend/", "/tab/",
)

# 主要城市 GPS 坐标 (纬度, 经度)
CITY_COORDS = {
    "北京": (39.9042, 116.4074),
    "上海": (31.2304, 121.4737),
    "广州": (23.1291, 113.2644),
    "深圳": (22.5431, 114.0579),
    "成都": (30.5728, 104.0668),
    "杭州": (30.2741, 120.1551),
    "武汉": (30.5928, 114.3055),
    "重庆": (29.4316, 106.9123),
    "南京": (32.0603, 118.7969),
    "天津": (39.3434, 117.3616),
    "苏州": (31.2990, 120.5853),
    "西安": (34.3416, 108.9398),
    "长沙": (28.2282, 112.9388),
    "沈阳": (41.8057, 123.4315),
    "青岛": (36.0671, 120.3826),
    "郑州": (34.7466, 113.6254),
    "大连": (38.9140, 121.6147),
    "东莞": (23.0430, 113.7633),
    "宁波": (29.8683, 121.5440),
    "厦门": (24.4798, 118.0894),
    "福州": (26.0745, 119.2965),
    "无锡": (31.4912, 120.3119),
    "合肥": (31.8206, 117.2272),
    "昆明": (25.0389, 102.7183),
    "哈尔滨": (45.8038, 126.5350),
    "济南": (36.6512, 117.1201),
    "佛山": (23.0218, 113.1219),
    "长春": (43.8171, 125.3235),
    "温州": (28.0001, 120.6722),
    "石家庄": (38.0428, 114.5149),
    "南宁": (22.8170, 108.3665),
    "常州": (31.8106, 119.9741),
    "泉州": (24.8741, 118.6757),
    "南昌": (28.6829, 115.8581),
    "贵阳": (26.6470, 106.6302),
    "太原": (37.8706, 112.5489),
    "烟台": (37.4638, 121.4479),
    "嘉兴": (30.7469, 120.7556),
    "南通": (31.9802, 120.8942),
    "珠海": (22.2710, 113.5767),
    "惠州": (23.1115, 114.4161),
    "徐州": (34.2058, 117.2862),
    "海口": (20.0174, 110.3492),
    "乌鲁木齐": (43.8256, 87.6168),
    "兰州": (36.0611, 103.8343),
    "潍坊": (36.7070, 119.1619),
    "保定": (38.8739, 115.4647),
    "扬州": (32.3949, 119.4145),
    "桂林": (25.2736, 110.2907),
    "三亚": (18.2528, 109.5120),
    "呼和浩特": (40.8414, 111.7519),
    "洛阳": (34.6197, 112.4540),
    "拉萨": (29.6525, 91.1721),
    "银川": (38.4872, 106.2309),
    "西宁": (36.6171, 101.7782),
}


def _get_city_coords(city: str):
    """Exact match → strip suffix → substring match."""
    if city in CITY_COORDS:
        return CITY_COORDS[city]
    for suffix in ("市", "区", "县"):
        stripped = city.rstrip(suffix)
        if stripped and stripped in CITY_COORDS:
            return CITY_COORDS[stripped]
    for name, coords in CITY_COORDS.items():
        if name in city or city in name:
            return coords
    return None


class DouyinCrawler(BaseCrawler):
    platform_name = "抖音"

    def __init__(self, browser_manager=None):
        super().__init__(browser_manager)
        self._session_ctx = None
        self._session_page = None

    async def cleanup_session(self):
        """Release cached browser session."""
        if self._session_page:
            try:
                if not self._session_page.is_closed():
                    await self._session_page.close()
            except Exception:
                pass
            self._session_page = None
        if self._session_ctx:
            try:
                await self._session_ctx.close()
            except Exception:
                pass
            self._session_ctx = None

    # ═══════════════════════════════════════════════════
    #  Main entry point
    # ═══════════════════════════════════════════════════

    async def fetch_tongcheng(self) -> list[CrawlResult]:
        if not self.bm:
            return []

        city = self.target_city
        if not city:
            self._notify("[抖音] 未设置目标城市，使用主页信息流采集")
            return await self._strategy_main_feed()

        coords = _get_city_coords(city)
        if not coords:
            self._notify(
                f"[抖音] 未找到城市「{city}」的坐标，使用主页信息流采集")
            return await self._strategy_main_feed()

        # ── Reuse cached session from previous cycle ──
        if self._session_page and not self._session_page.is_closed():
            try:
                self._notify("[抖音] 复用已有浏览器会话...")
                results = await self._do_feed_collect(
                    self._session_page, is_refresh=True)
                if results:
                    self._notify(
                        f"[抖音] 复用会话成功！获取 {len(results)} 条数据")
                    return results
                self._notify("[抖音] 复用会话未获取到新数据，重新创建...")
            except Exception as exc:
                logger.warning("[抖音] Session reuse error: %s", exc)
            await self.cleanup_session()

        # ── Cascade strategies ──
        strategies = [
            ("方案1: 地理位置伪装采集(自动)",
             lambda: self._strategy_geo_headless(coords)),
            ("方案2: 移动端模拟采集(手动验证)",
             lambda: self._strategy_mobile_headed(coords)),
        ]

        for name, fn in strategies:
            self._notify(f"[抖音] 正在尝试 {name}...")
            try:
                results = await fn()
                if results:
                    self._notify(
                        f"[抖音] {name} 成功！获取 {len(results)} 条同城数据")
                    return results
                self._notify(
                    f"[抖音] {name} 未获取到数据，准备尝试下一方案")
            except Exception as exc:
                logger.warning("[抖音] %s error: %s", name, exc)
                self._notify(
                    f"[抖音] {name} 出错: {exc}，准备尝试下一方案")

        self._notify("[抖音] 同城方案均未成功，使用主页信息流兜底采集")
        return await self._strategy_main_feed()

    # ═══════════════════════════════════════════════════
    #  S1: Desktop + geolocation (headless)
    # ═══════════════════════════════════════════════════

    async def _strategy_geo_headless(self, coords) -> list[CrawlResult]:
        """Desktop browser with GPS spoofed to target city. Fast, headless."""
        await self.cleanup_session()
        ctx, page = await self.bm.create_geo_context(
            self.platform_name, coords[0], coords[1])

        results = await self._do_feed_collect(page, is_refresh=False)
        if results:
            self._session_ctx = ctx
            self._session_page = page
            await self._save_session_cookies()
        else:
            try:
                await page.close()
                await ctx.close()
            except Exception:
                pass
        return results

    # ═══════════════════════════════════════════════════
    #  S2: Mobile emulation + geo (headed, for CAPTCHA)
    # ═══════════════════════════════════════════════════

    async def _strategy_mobile_headed(self, coords) -> list[CrawlResult]:
        """Mobile browser with GPS, headed for manual CAPTCHA solving.

        After success, saves cookies and closes the headed browser.
        Next cycle, S1 will reuse these cookies in headless mode.
        """
        ctx = None
        try:
            ctx, page = await self.bm.create_persistent_context(
                self.platform_name, headless=False,
                geo_coords=coords, mobile=True)
            results = await self._do_feed_collect(
                page, is_refresh=False, wait_for_captcha=True)
            if results:
                try:
                    cookies = await ctx.cookies()
                    if cookies:
                        self.bm._cookie_path(self.platform_name).write_text(
                            json.dumps(cookies, ensure_ascii=False, indent=2),
                            encoding="utf-8")
                except Exception:
                    pass
            return results
        finally:
            if ctx:
                try:
                    await ctx.close()
                except Exception:
                    pass

    # ═══════════════════════════════════════════════════
    #  Core feed collection (shared by all strategies)
    # ═══════════════════════════════════════════════════

    async def _do_feed_collect(self, page, *, is_refresh=False,
                               wait_for_captcha=False) -> list[CrawlResult]:
        """Navigate to Douyin, try 同城 tab, scroll, intercept APIs."""
        captured = []

        async def on_response(response):
            if response.status != 200:
                return
            url = response.url
            if not any(p in url for p in FEED_API_PATTERNS):
                return
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct or "javascript" in ct:
                    body = await response.json()
                    captured.append(body)
                    logger.debug("[抖音] API: %s", url.split("?")[0][-60:])
            except Exception:
                pass

        page.on("response", on_response)
        ssr_items = []

        try:
            if is_refresh:
                await page.reload(
                    wait_until="domcontentloaded", timeout=30_000)
            else:
                await page.goto(
                    DOUYIN_URL, wait_until="domcontentloaded", timeout=30_000)
            await human_delay(3.0, 5.0)

            if await self._detect_captcha(page):
                if wait_for_captcha:
                    self._notify(
                        "[抖音] 检测到验证码，请在弹出的浏览器中手动完成验证"
                        "（包括图案验证和手机号验证）...")
                    for _ in range(90):
                        await asyncio.sleep(2)
                        if not await self._detect_captcha(page):
                            self._notify("[抖音] 验证已通过！正在采集...")
                            await human_delay(2, 4)
                            break
                    else:
                        self._notify("[抖音] 验证码等待超时")
                        return []
                else:
                    return []

            clicked = await self._click_tongcheng_tab(page)
            if clicked:
                self._notify("[抖音] 已进入同城/附近页面，正在采集...")
                await human_delay(3, 5)

            ssr_items = await self._extract_ssr(page)
            await human_scroll(page, times=8, jitter=True)
            await human_delay(2, 3)

        except Exception as exc:
            logger.warning("[抖音] Feed collect error: %s", exc)
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

        logger.info("[抖音] Collected %d items (SSR=%d, API=%d)",
                    len(results), len(ssr_items), len(captured))
        return results

    # ═══════════════════════════════════════════════════
    #  Click 同城/附近 tab
    # ═══════════════════════════════════════════════════

    async def _click_tongcheng_tab(self, page) -> bool:
        for label in ("同城", "附近", "本地"):
            for sel in [
                f'a:has-text("{label}")',
                f'div[role="tab"]:has-text("{label}")',
                f'span:has-text("{label}")',
                f'button:has-text("{label}")',
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2000):
                        await el.click()
                        await human_delay(2, 4)
                        logger.info("[抖音] Clicked '%s' tab", label)
                        return True
                except Exception:
                    continue

        try:
            clicked = await page.evaluate("""() => {
                const targets = ['同城', '附近', '本地'];
                for (const el of document.querySelectorAll(
                    'a, div, span, li, button, nav *')) {
                    const t = (el.textContent || '').trim();
                    for (const target of targets) {
                        if (t === target) { el.click(); return t; }
                    }
                }
                return null;
            }""")
            if clicked:
                await human_delay(2, 4)
                logger.info("[抖音] Clicked '%s' via JS", clicked)
                return True
        except Exception:
            pass
        return False

    # ═══════════════════════════════════════════════════
    #  CAPTCHA detection
    # ═══════════════════════════════════════════════════

    async def _detect_captcha(self, page) -> bool:
        try:
            title = (await page.title()).lower()
            url = page.url.lower()
            if any(k in title for k in CAPTCHA_INDICATORS_TITLE):
                return True
            if any(k in url for k in CAPTCHA_INDICATORS_URL):
                return True
            sel = ", ".join(CAPTCHA_SELECTORS)
            cap = await page.query_selector(sel)
            return cap is not None
        except Exception:
            return False

    # ═══════════════════════════════════════════════════
    #  Session cookie persistence
    # ═══════════════════════════════════════════════════

    async def _save_session_cookies(self):
        if self._session_ctx:
            try:
                cookies = await self._session_ctx.cookies()
                if cookies and self.bm:
                    self.bm._cookie_path(self.platform_name).write_text(
                        json.dumps(cookies, ensure_ascii=False, indent=2),
                        encoding="utf-8")
            except Exception:
                pass

    # ═══════════════════════════════════════════════════
    #  Fallback: Main feed (no city filter)
    # ═══════════════════════════════════════════════════

    async def _strategy_main_feed(self) -> list[CrawlResult]:
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
            except Exception:
                pass

        page.on("response", on_response)

        try:
            await page.goto(
                DOUYIN_URL, wait_until="domcontentloaded", timeout=30_000)
            await human_delay(4.0, 6.0)
            ssr_items = await self._extract_ssr(page)
            await human_scroll(page, times=6, jitter=True)
            await human_delay(1.0, 2.0)
        except Exception as exc:
            logger.warning("[抖音] Feed error: %s", exc)
            ssr_items = []
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
