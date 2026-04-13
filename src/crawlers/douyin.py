"""
抖音爬虫 — 同城话题搜索采集

抖音网页版（桌面端）没有「同城」标签页，该功能仅存在于手机 App。
但抖音创作者会主动为本地内容打上 #城市同城 等话题标签，这些视频
与 App 同城页展示的内容高度重合。

采集策略：
  1. 搜索「{城市}同城」话题 — 获取创作者为本地受众标记的视频
  2. 搜索「{城市}生活」— 补充本地生活类内容
  3. 辅以 GPS 地理位置伪装 — 尽可能影响推荐算法
  4. 会话缓存 — 验证码只需过一次，后续自动复用 Cookie

策略级联：
  方案1: 自动搜索（无头浏览器 + 地理位置伪装，全自动）
  方案2: 手动验证（有头浏览器，用户过一次验证码后自动保存）
  兜底:  主页信息流（无城市过滤）
"""
import asyncio
import json
import logging
from datetime import datetime
from urllib.parse import quote, unquote

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

SEARCH_API_PATTERNS = (
    "/aweme/", "/search/", "/feed/", "/nearby/",
    "/same_city/", "/recommend/", "/tab/",
)

# 搜索词模板（按优先级排列）
SEARCH_TEMPLATES = [
    "{city}同城",
    "{city}生活",
]

# GPS 坐标（可选，用于辅助地理位置伪装）
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
    # 内蒙古
    "呼伦贝尔": (49.2122, 119.7653),
    "包头": (40.6571, 109.8403),
    "鄂尔多斯": (39.6086, 109.7810),
    "赤峰": (42.2581, 118.8870),
    "通辽": (43.6175, 122.2435),
    # 东北
    "鞍山": (41.1087, 122.9956),
    "吉林": (43.8380, 126.5496),
    "齐齐哈尔": (47.3542, 123.9180),
    "大庆": (46.5907, 125.1040),
    "牡丹江": (44.5522, 129.6329),
    "佳木斯": (46.7996, 130.3211),
    # 四川
    "绵阳": (31.4680, 104.6797),
    "德阳": (31.1289, 104.3979),
    "宜宾": (28.7513, 104.6417),
    "南充": (30.8373, 106.1107),
    "乐山": (29.5521, 103.7660),
    "泸州": (28.8717, 105.4425),
    # 湖北
    "宜昌": (30.6913, 111.2864),
    "襄阳": (32.0086, 112.1222),
    "荆州": (30.3263, 112.2390),
    # 湖南
    "岳阳": (29.3572, 113.1288),
    "衡阳": (26.8931, 112.5720),
    "株洲": (27.8274, 113.1339),
    # 广西
    "柳州": (24.3264, 109.4283),
    "北海": (21.4814, 109.1196),
    # 河北
    "秦皇岛": (39.9354, 119.5991),
    "邢台": (37.0706, 114.5047),
    "承德": (40.9510, 117.9332),
    "张家口": (40.8240, 114.8870),
    "廊坊": (39.5186, 116.6837),
    "唐山": (39.6309, 118.1802),
    "邯郸": (36.6116, 114.5312),
    # 山东
    "临沂": (35.1041, 118.3563),
    "淄博": (36.8131, 118.0549),
    "泰安": (36.1952, 117.1209),
    "威海": (37.5131, 122.1200),
    "日照": (35.4164, 119.5269),
    "济宁": (35.4147, 116.5872),
    "聊城": (36.4568, 115.9855),
    "德州": (37.4351, 116.3575),
    # 江苏
    "连云港": (34.5960, 119.1789),
    "淮安": (33.6065, 119.0153),
    "盐城": (33.3496, 120.1634),
    "泰州": (32.4906, 119.9231),
    "镇江": (32.1879, 119.4250),
    "宿迁": (33.9631, 118.2750),
    # 安徽
    "芜湖": (31.3529, 118.4330),
    "蚌埠": (32.9169, 117.3894),
    "阜阳": (32.8903, 115.8149),
    "安庆": (30.5435, 117.0630),
    "马鞍山": (31.6705, 118.5065),
    "黄山": (29.7147, 118.3380),
    # 福建
    "漳州": (24.5126, 117.6470),
    "莆田": (25.4312, 119.0078),
    "龙岩": (25.0756, 117.0171),
    # 江西
    "赣州": (25.8454, 114.9336),
    "上饶": (28.4545, 117.9435),
    "九江": (29.7048, 116.0013),
    "景德镇": (29.2688, 117.1784),
    # 云南
    "大理": (25.6065, 100.2676),
    "丽江": (26.8728, 100.2258),
    "曲靖": (25.4895, 103.7948),
    # 贵州
    "遵义": (27.7254, 106.9273),
    # 广东
    "汕头": (23.3535, 116.6814),
    "江门": (22.5788, 113.0819),
    "湛江": (21.2707, 110.3594),
    "茂名": (21.6631, 110.9255),
    "肇庆": (23.0471, 112.4651),
    "清远": (23.6819, 113.0561),
    "中山": (22.5176, 113.3926),
    "揭阳": (23.5497, 116.3728),
    "韶关": (24.8011, 113.5975),
    # 陕西
    "咸阳": (34.3293, 108.7090),
    "宝鸡": (34.3614, 107.2372),
    "渭南": (34.4996, 109.5099),
    "汉中": (33.0674, 107.0236),
    "延安": (36.5853, 109.4894),
    # 甘肃
    "天水": (34.5809, 105.7249),
    # 新疆
    "克拉玛依": (45.5789, 84.8892),
    "库尔勒": (41.7259, 86.1747),
    "喀什": (39.4676, 75.9896),
    "伊宁": (43.9088, 81.3297),
    # 海南
    "儋州": (19.5175, 109.5809),
    # 西藏
    "日喀则": (29.2669, 88.8799),
}


def _get_city_coords(city: str):
    """Exact match → strip suffix → substring match."""
    if city in CITY_COORDS:
        return CITY_COORDS[city]
    for suffix in ("市", "区", "县", "盟", "州"):
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

    async def cleanup_session(self):
        """Release cached browser context."""
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

        # ── Reuse cached context (cookies survive across cycles) ──
        if self._session_ctx:
            try:
                self._notify("[抖音] 复用已有会话，搜索同城内容...")
                page = await self._session_ctx.new_page()
                await apply_stealth(page)
                try:
                    results = await self._search_city_content(page, city)
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass
                if results:
                    self._notify(
                        f"[抖音] 复用会话成功！获取 {len(results)} 条同城内容")
                    return results
                self._notify("[抖音] 复用会话未获取到新数据，重新创建...")
            except Exception as exc:
                logger.warning("[抖音] Session reuse error: %s", exc)
            await self.cleanup_session()

        # ── Cascade strategies ──
        strategies = [
            ("方案1: 同城内容采集(自动)",
             lambda: self._strategy_search_headless(city, coords)),
            ("方案2: 同城内容采集(手动验证)",
             lambda: self._strategy_search_headed(city, coords)),
        ]

        for name, fn in strategies:
            self._notify(f"[抖音] 正在尝试 {name}...")
            try:
                results = await fn()
                if results:
                    self._notify(
                        f"[抖音] {name} 成功！获取 {len(results)} 条同城内容")
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
    #  S1: Search headless (automatic)
    # ═══════════════════════════════════════════════════

    async def _strategy_search_headless(self, city, coords
                                        ) -> list[CrawlResult]:
        """Headless browser search for city hashtags. Fully automatic."""
        await self.cleanup_session()

        if coords:
            ctx, page = await self.bm.create_geo_context(
                self.platform_name, coords[0], coords[1])
        else:
            page = await self.bm.create_stealth_page(self.platform_name)
            ctx = None

        try:
            results = await self._search_city_content(page, city)
        finally:
            try:
                await page.close()
            except Exception:
                pass

        if results and ctx:
            self._session_ctx = ctx
            self._save_cookies_from_ctx(ctx)
        elif ctx:
            try:
                await ctx.close()
            except Exception:
                pass

        return results

    # ═══════════════════════════════════════════════════
    #  S2: Search headed (manual CAPTCHA)
    # ═══════════════════════════════════════════════════

    async def _strategy_search_headed(self, city, coords
                                      ) -> list[CrawlResult]:
        """Headed browser for manual CAPTCHA. Saves cookies for next cycle."""
        ctx = None
        try:
            ctx, page = await self.bm.create_persistent_context(
                self.platform_name, headless=False,
                geo_coords=coords, mobile=True)
            results = await self._search_city_content(
                page, city, wait_for_captcha=True)
            if results:
                self._save_cookies_from_ctx(ctx)
            return results
        finally:
            if ctx:
                try:
                    await ctx.close()
                except Exception:
                    pass

    # ═══════════════════════════════════════════════════
    #  Core: search for city-specific hashtag content
    # ═══════════════════════════════════════════════════

    async def _search_city_content(self, page, city, *,
                                   wait_for_captcha=False
                                   ) -> list[CrawlResult]:
        """Search Douyin for '{city}同城', '{city}生活' etc.

        These hashtag-based searches return videos that creators tagged
        for the local audience — essentially the same pool as the app's
        同城 tab.
        """
        all_results: list[CrawlResult] = []
        seen: set = set()

        for template in SEARCH_TEMPLATES:
            term = template.format(city=city)
            search_url = (
                f"https://www.douyin.com/search/{quote(term)}"
                f"?type=video"
            )
            self._notify(f"[抖音] 搜索「{term}」...")

            results = await self._do_search_page(
                page, search_url, seen,
                wait_for_captcha=wait_for_captcha)
            all_results.extend(results)

            if len(all_results) >= 40:
                break

            await human_delay(2, 4)

        logger.info("[抖音] City search total: %d items for '%s'",
                    len(all_results), city)
        return all_results

    async def _do_search_page(self, page, search_url, seen, *,
                              wait_for_captcha=False
                              ) -> list[CrawlResult]:
        """Navigate to a search URL, intercept APIs, parse results."""
        captured = []

        async def on_response(response):
            if response.status != 200:
                return
            url = response.url
            if not any(p in url for p in SEARCH_API_PATTERNS):
                return
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct or "javascript" in ct:
                    body = await response.json()
                    captured.append(body)
                    logger.debug("[抖音] API: %s",
                                 url.split("?")[0][-60:])
            except Exception:
                pass

        page.on("response", on_response)

        try:
            await page.goto(
                search_url, wait_until="domcontentloaded", timeout=30_000)
            await human_delay(3.0, 5.0)

            if await self._detect_captcha(page):
                if wait_for_captcha:
                    self._notify(
                        "[抖音] 检测到验证码，请在浏览器中手动完成"
                        "（图案验证 + 手机号验证均需完成）...")
                    for _ in range(90):
                        await asyncio.sleep(2)
                        if not await self._detect_captcha(page):
                            self._notify("[抖音] 验证已通过！继续采集...")
                            await human_delay(2, 4)
                            await page.goto(
                                search_url,
                                wait_until="domcontentloaded",
                                timeout=30_000)
                            await human_delay(3, 5)
                            break
                    else:
                        self._notify("[抖音] 验证码等待超时")
                        return []
                else:
                    return []

            ssr_items = await self._extract_ssr(page)
            await human_scroll(page, times=6, jitter=True)
            await human_delay(1.5, 3.0)

        except Exception as exc:
            logger.warning("[抖音] Search page error: %s", exc)
            return []
        finally:
            page.remove_listener("response", on_response)

        results = []
        for item in ssr_items:
            r = self._parse_aweme(item, seen)
            if r:
                results.append(r)
        for body in captured:
            results.extend(self._parse_any_response(body, seen))
        return results

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
    #  Cookie persistence
    # ═══════════════════════════════════════════════════

    def _save_cookies_from_ctx(self, ctx):
        """Best-effort sync cookie save (fire and forget)."""
        import asyncio as _aio
        async def _save():
            try:
                cookies = await ctx.cookies()
                if cookies and self.bm:
                    self.bm._cookie_path(self.platform_name).write_text(
                        json.dumps(cookies, ensure_ascii=False, indent=2),
                        encoding="utf-8")
            except Exception:
                pass
        try:
            loop = _aio.get_running_loop()
            loop.create_task(_save())
        except Exception:
            pass

    # ═══════════════════════════════════════════════════
    #  Fallback: Main feed
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
            if "/aweme/" not in response.url:
                return
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct or "javascript" in ct:
                    captured.append(await response.json())
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
                    data = json.loads(unquote(text))
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

    # ── Response parsers (unchanged) ──

    def _parse_any_response(self, body: dict, seen: set) -> list[CrawlResult]:
        if not isinstance(body, dict):
            return []
        return [r for aweme in self._extract_awemes(body)
                if (r := self._parse_aweme(aweme, seen))]

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
                if "desc" in item and (
                    "author" in item or "nickname" in item
                ):
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

        return CrawlResult(
            platform="抖音",
            item_id=aweme_id,
            nickname=nickname,
            content=content[:500],
            link=share_url,
            publish_date=self._ts_to_str(ts),
        )

    @staticmethod
    def _ts_to_str(ts) -> str:
        if not ts:
            return ""
        try:
            return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return ""
