"""
抖音同城 API 客户端 — 真实同城内容

通过 TikHub API 调用抖音「同城热点榜」接口，获取与 App
同城频道高度一致的城市热门内容。

使用方法：
  1. 注册 TikHub  →  https://user.tikhub.io/users/signin
  2. 验证邮箱后每日签到获取免费额度
  3. 创建 API Token
  4. 在工具「设置 → 抖音同城API」中填入 Token
"""
import logging
from datetime import datetime
from typing import Optional

import httpx

from .base import CrawlResult

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.tikhub.io"
CN_API_BASE = "https://api.tikhub.dev"

# ═══════════════════════════════════════════════════════════
#  City code mapping (行政区划代码 — used by TikHub & Douyin)
# ═══════════════════════════════════════════════════════════

DOUYIN_CITY_CODES: dict[str, str] = {
    # 直辖市
    "北京": "110000", "天津": "120000", "上海": "310000", "重庆": "500000",
    # 河北
    "石家庄": "130100", "唐山": "130200", "秦皇岛": "130300",
    "邯郸": "130400", "邢台": "130500", "保定": "130600",
    "张家口": "130700", "承德": "130800", "廊坊": "131000",
    # 山西
    "太原": "140100",
    # 内蒙古
    "呼和浩特": "150100", "包头": "150200", "赤峰": "150400",
    "鄂尔多斯": "150600", "呼伦贝尔": "150700", "通辽": "150500",
    # 辽宁
    "沈阳": "210100", "大连": "210200", "鞍山": "210300",
    # 吉林
    "长春": "220100", "吉林": "220200",
    # 黑龙江
    "哈尔滨": "230100", "齐齐哈尔": "230200", "大庆": "230600",
    "牡丹江": "231000", "佳木斯": "230800",
    # 江苏
    "南京": "320100", "无锡": "320200", "徐州": "320300",
    "常州": "320400", "苏州": "320500", "南通": "320600",
    "连云港": "320700", "淮安": "320800", "盐城": "320900",
    "扬州": "321000", "镇江": "321100", "泰州": "321200",
    "宿迁": "321300",
    # 浙江
    "杭州": "330100", "宁波": "330200", "温州": "330300",
    "嘉兴": "330400",
    # 安徽
    "合肥": "340100", "芜湖": "340200", "蚌埠": "340300",
    "阜阳": "341200", "安庆": "340800", "马鞍山": "340500",
    "黄山": "341000",
    # 福建
    "福州": "350100", "厦门": "350200", "泉州": "350500",
    "漳州": "350600", "莆田": "350300", "龙岩": "350800",
    # 江西
    "南昌": "360100", "赣州": "360700", "上饶": "361100",
    "九江": "360400", "景德镇": "360200",
    # 山东
    "济南": "370100", "青岛": "370200", "烟台": "370600",
    "潍坊": "370700", "临沂": "371300", "淄博": "370300",
    "泰安": "370900", "威海": "371000", "日照": "371100",
    "济宁": "370800", "聊城": "371500", "德州": "371400",
    # 河南
    "郑州": "410100", "洛阳": "410300",
    # 湖北
    "武汉": "420100", "宜昌": "420500", "襄阳": "420600",
    "荆州": "421000",
    # 湖南
    "长沙": "430100", "岳阳": "430600", "衡阳": "430400",
    "株洲": "430200",
    # 广东
    "广州": "440100", "深圳": "440300", "东莞": "441900",
    "佛山": "440600", "珠海": "440400", "惠州": "441300",
    "汕头": "440500", "江门": "440700", "湛江": "440800",
    "茂名": "440900", "肇庆": "441200", "清远": "441800",
    "中山": "442000", "揭阳": "445200", "韶关": "440200",
    # 广西
    "南宁": "450100", "柳州": "450200", "桂林": "450300",
    "北海": "450500",
    # 海南
    "海口": "460100", "三亚": "460200", "儋州": "460400",
    # 四川
    "成都": "510100", "绵阳": "510700", "德阳": "510600",
    "宜宾": "511500", "南充": "511300", "乐山": "511100",
    "泸州": "510500",
    # 贵州
    "贵阳": "520100", "遵义": "520300",
    # 云南
    "昆明": "530100", "大理": "532900", "丽江": "530700",
    "曲靖": "530300",
    # 西藏
    "拉萨": "540100", "日喀则": "540200",
    # 陕西
    "西安": "610100", "咸阳": "610400", "宝鸡": "610300",
    "渭南": "610500", "汉中": "610700", "延安": "610600",
    # 甘肃
    "兰州": "620100", "天水": "620500",
    # 青海
    "西宁": "630100",
    # 宁夏
    "银川": "640100",
    # 新疆
    "乌鲁木齐": "650100", "克拉玛依": "650200",
    "库尔勒": "652800", "喀什": "653100", "伊宁": "654000",
}


def _get_city_code(city: str) -> Optional[str]:
    """Fuzzy-match city name to 6-digit administrative code."""
    if city in DOUYIN_CITY_CODES:
        return DOUYIN_CITY_CODES[city]
    for suffix in ("市", "区", "县", "盟", "州"):
        stripped = city.rstrip(suffix)
        if stripped and stripped in DOUYIN_CITY_CODES:
            return DOUYIN_CITY_CODES[stripped]
    for name, code in DOUYIN_CITY_CODES.items():
        if name in city or city in name:
            return code
    return None


# ═══════════════════════════════════════════════════════════
#  TikHub API client — 同城热点榜
# ═══════════════════════════════════════════════════════════

async def fetch_nearby_videos(
    token: str,
    city: str,
    *,
    api_base: str = "",
    notify=None,
    max_pages: int = 3,
    page_size: int = 20,
) -> list[CrawlResult]:
    """Fetch city hot-list videos from TikHub's Douyin Billboard API.

    Paginates up to *max_pages* and returns deduplicated CrawlResult list.
    """
    city_code = _get_city_code(city)
    if not city_code:
        if notify:
            notify(f"[抖音API] 未找到城市「{city}」的城市代码，跳过API采集")
        logger.warning("[抖音API] Unknown city code for: %s", city)
        return []

    base = (api_base.strip().rstrip("/") if api_base else "") or DEFAULT_API_BASE
    headers = {"Authorization": f"Bearer {token}"}

    all_results: list[CrawlResult] = []
    seen: set[str] = set()

    for page_num in range(1, max_pages + 1):
        if notify:
            notify(f"[抖音API] 同城热点第 {page_num} 页"
                   f"（城市: {city}, 代码: {city_code}）...")

        params = {
            "city_code": city_code,
            "page": page_num,
            "page_size": page_size,
            "order": "rank",
        }

        try:
            async with httpx.AsyncClient(
                timeout=30, verify=False,
            ) as client:
                resp = await client.get(
                    f"{base}/api/v1/douyin/billboard/fetch_hot_city_list",
                    params=params, headers=headers,
                )
                data = resp.json()
        except httpx.TimeoutException:
            logger.warning("[抖音API] Timeout on page %d", page_num)
            if notify:
                notify("[抖音API] 请求超时")
            break
        except Exception as exc:
            logger.warning("[抖音API] Request error page %d: %s",
                           page_num, exc)
            if notify:
                notify(f"[抖音API] 请求失败: {exc}")
            break

        # TikHub error responses: {"detail": {"code": 401, ...}}
        # TikHub success responses: {"code": 200, "data": {...}}
        detail = data.get("detail", {})
        detail_code = detail.get("code") if isinstance(detail, dict) else None
        top_code = data.get("code")

        if detail_code == 401 or top_code == 401:
            msg = (detail.get("message_zh", "")
                   or detail.get("message", "") if isinstance(detail, dict)
                   else "Token 无效")
            if notify:
                notify(f"[抖音API] Token 无效: {msg}")
            logger.warning("[抖音API] Auth error: %s", msg)
            break
        if detail_code == 402 or top_code == 402:
            msg = (detail.get("message_zh", "")
                   or detail.get("message", "") if isinstance(detail, dict)
                   else "额度不足")
            if notify:
                notify(f"[抖音API] 额度不足: {msg}")
            logger.warning("[抖音API] Insufficient balance: %s", msg)
            break

        items = _parse_tikhub_response(data, seen)
        all_results.extend(items)

        logger.info("[抖音API] Page %d: %d items (total %d)",
                    page_num, len(items), len(all_results))

        if not items:
            break

    if all_results and notify:
        notify(f"[抖音API] 同城热点成功！共获取 {len(all_results)} 条城市热门内容")

    return all_results


# ═══════════════════════════════════════════════════════════
#  Response parsing
# ═══════════════════════════════════════════════════════════

def _parse_tikhub_response(
    data: dict,
    seen: set[str],
) -> list[CrawlResult]:
    """Parse TikHub billboard/hot-city-list response."""
    if not isinstance(data, dict):
        return []

    # TikHub standard wrapper: {code, router, data: {data: {...}}}
    inner = data.get("data", data)
    if isinstance(inner, dict) and "data" in inner:
        inner = inner["data"]

    # The hot list may be under various keys
    item_list = None
    if isinstance(inner, dict):
        for key in ("objs", "word_list", "wordList", "hot_list", "hotList",
                     "trending_list", "trendingList", "data",
                     "aweme_list", "awemeList", "list", "items"):
            val = inner.get(key)
            if isinstance(val, list) and val:
                item_list = val
                break
        if item_list is None and isinstance(inner, list):
            item_list = inner
    elif isinstance(inner, list):
        item_list = inner

    if not item_list:
        logger.info("[抖音API] No items found. Keys: %s",
                    list(inner.keys() if isinstance(inner, dict)
                         else data.keys())[:8])
        return []

    results: list[CrawlResult] = []
    for item in item_list:
        r = _parse_hot_item(item, seen)
        if r:
            results.append(r)
    return results


def _parse_hot_item(
    item: dict,
    seen: set[str],
) -> Optional[CrawlResult]:
    """Parse a single hot-list item (may be aweme or word/topic format)."""
    if not isinstance(item, dict):
        return None

    # TikHub billboard items often have aweme_info or video_info
    aweme = (item.get("aweme_info")
             or item.get("video_info")
             or item.get("aweme")
             or item)

    if isinstance(aweme, dict) and "aweme_id" not in aweme:
        # Could be a word/topic item with nested video
        for nested_key in ("related_awemes", "video_list",
                           "aweme_list", "videos"):
            nested = aweme.get(nested_key)
            if isinstance(nested, list) and nested:
                aweme = nested[0]
                break

    # Extract aweme_id
    aweme_id = str(
        aweme.get("aweme_id", "")
        or aweme.get("awemeId", "")
        or aweme.get("id", "")
        or item.get("sentence_id", "")
        or item.get("hot_value", "")
    )
    if not aweme_id or aweme_id in seen:
        # For hot-list items without a clear ID, use the word/title
        word = item.get("word", "") or item.get("title", "")
        if word and word not in seen:
            aweme_id = word
        else:
            return None
    seen.add(aweme_id)

    author = aweme.get("author") or aweme.get("authorInfo") or {}
    nickname = author.get("nickname", "") or author.get("name", "")
    if not nickname:
        city_name = item.get("city_name", "")
        tag_name = item.get("sentence_tag_name", "")
        nickname = city_name or tag_name or ""

    content = (
        aweme.get("desc", "")
        or aweme.get("title", "")
        or item.get("sentence", "")
        or item.get("word", "")
        or "(无文案)"
    )
    hot_score = item.get("hot_score") or item.get("hot_value")
    if hot_score and content != "(无文案)":
        content = f"{content}  [热度: {hot_score}]"

    share_url = (
        aweme.get("share_url", "")
        or aweme.get("shareUrl", "")
    )
    if not share_url:
        sentence = item.get("sentence", "") or item.get("word", "")
        if aweme_id and str(aweme_id).isdigit() and len(str(aweme_id)) > 6:
            share_url = f"https://www.douyin.com/video/{aweme_id}"
        elif sentence:
            from urllib.parse import quote as _url_quote
            share_url = (f"https://www.douyin.com/search/"
                         f"{_url_quote(sentence)}")
        else:
            share_url = f"https://www.douyin.com/search/{content[:20]}"

    ts = (aweme.get("create_time", 0)
          or aweme.get("createTime", 0)
          or item.get("create_at", 0))
    pub_date = ""
    if ts:
        try:
            pub_date = datetime.fromtimestamp(int(ts)).strftime(
                "%Y-%m-%d %H:%M")
        except Exception:
            pass

    return CrawlResult(
        platform="抖音",
        item_id=aweme_id,
        nickname=nickname,
        content=content[:500],
        link=share_url,
        publish_date=pub_date,
    )


# ═══════════════════════════════════════════════════════════
#  Connection test
# ═══════════════════════════════════════════════════════════

async def test_api_connection(
    token: str,
    city: str = "北京",
    api_base: str = "",
) -> tuple[bool, str]:
    """Quick connectivity / token validation test."""
    base = (api_base.strip().rstrip("/") if api_base else "") or DEFAULT_API_BASE
    city_code = _get_city_code(city) or "110000"
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "city_code": city_code,
        "page": 1,
        "page_size": 5,
        "order": "rank",
    }
    try:
        async with httpx.AsyncClient(
            timeout=15, verify=False,
        ) as client:
            resp = await client.get(
                f"{base}/api/v1/douyin/billboard/fetch_hot_city_list",
                params=params, headers=headers,
            )
            data = resp.json()
    except httpx.TimeoutException:
        return False, "请求超时，请检查网络连接"
    except Exception as exc:
        return False, f"连接失败: {exc}"

    detail = data.get("detail", {})
    detail_code = detail.get("code") if isinstance(detail, dict) else None
    top_code = data.get("code")

    if detail_code == 401 or top_code == 401:
        msg = (detail.get("message_zh", "")
               or detail.get("message", "") if isinstance(detail, dict)
               else "")
        return False, f"Token 无效: {msg}"
    if detail_code == 402 or top_code == 402:
        msg = (detail.get("message_zh", "")
               or detail.get("message", "") if isinstance(detail, dict)
               else "")
        return False, f"额度不足: {msg}"

    # Try to parse actual items
    seen: set[str] = set()
    items = _parse_tikhub_response(data, seen)
    if items:
        return True, f"连接成功！返回 {len(items)} 条同城热门内容"
    if top_code in (200, 0):
        return True, "连接成功（API正常，但该城市暂无热点数据）"
    return False, f"接口返回异常: code={top_code or detail_code}"
