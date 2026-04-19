"""
微信视频号 API 客户端 — 城市相关内容采集

通过 TikHub API 搜索微信视频号中与城市相关的内容。
注意：此接口需要 TikHub 付费余额（$0.01/次），免费额度不适用。

使用方法：
  1. 注册 TikHub  →  https://user.tikhub.io/users/signin
  2. 在 TikHub 充值余额（Pricing 页面）
  3. 创建 API Token
  4. 在工具「设置 → 抖音同城API」中填入 Token（共用同一个 Token）
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from .base import CrawlResult

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.tikhub.io"

SEARCH_TEMPLATES = [
    "{city}同城",
    "{city}生活",
    "{city}本地",
    "{city}探店",
    "{city}美食",
]


async def fetch_wechat_city_videos(
    token: str,
    city: str,
    *,
    api_base: str = "",
    notify=None,
    max_keywords: int = 3,
    page_size: int = 20,
) -> list[CrawlResult]:
    """Search WeChat Channels for city-related videos via TikHub API.

    Uses keyword search with city-specific terms. Tries multiple keywords
    and deduplicates results. Returns early on 402 (insufficient balance).
    """
    base = (api_base.strip().rstrip("/") if api_base else "") or DEFAULT_API_BASE
    headers = {"Authorization": f"Bearer {token}"}
    all_results: list[CrawlResult] = []
    seen: set[str] = set()

    for i, template in enumerate(SEARCH_TEMPLATES[:max_keywords]):
        keyword = template.format(city=city)
        if notify:
            notify(f"[视频号API] 搜索「{keyword}」（第 {i+1} 组关键词）...")

        params = {
            "keyword": keyword,
            "page": 0,
            "page_size": page_size,
        }

        try:
            async with httpx.AsyncClient(
                timeout=30, verify=False,
            ) as client:
                resp = await client.get(
                    f"{base}/api/v1/wechat_channels/fetch_search_ordinary",
                    params=params, headers=headers,
                )
                data = resp.json()
        except httpx.TimeoutException:
            logger.warning("[视频号API] Timeout searching '%s'", keyword)
            if notify:
                notify("[视频号API] 请求超时")
            continue
        except Exception as exc:
            logger.warning("[视频号API] Request error: %s", exc)
            if notify:
                notify(f"[视频号API] 请求失败: {exc}")
            break

        detail = data.get("detail", {})
        detail_code = detail.get("code") if isinstance(detail, dict) else None
        top_code = data.get("code")

        if detail_code == 401 or top_code == 401:
            msg = (_detail_msg(detail) or "Token 无效")
            if notify:
                notify(f"[视频号API] Token 无效: {msg}")
            break
        if detail_code == 402 or top_code == 402:
            msg = (_detail_msg(detail)
                   or "余额不足（视频号接口需付费余额）")
            if notify:
                notify(f"[视频号API] {msg}")
            logger.info("[视频号API] 402 — paid endpoint, insufficient balance")
            break

        items = _parse_search_response(data, seen)
        all_results.extend(items)

        logger.info("[视频号API] '%s': %d items (total %d)",
                    keyword, len(items), len(all_results))

        if len(all_results) >= 40:
            break

    if all_results and notify:
        notify(f"[视频号API] 搜索成功！共获取 {len(all_results)} 条城市相关视频号内容")

    return all_results


def _detail_msg(detail) -> str:
    if not isinstance(detail, dict):
        return ""
    return (detail.get("message_zh", "")
            or detail.get("message", ""))


def _parse_search_response(
    data: dict,
    seen: set[str],
) -> list[CrawlResult]:
    """Parse TikHub WeChat Channels search response."""
    if not isinstance(data, dict):
        return []

    inner = data.get("data", data)
    if isinstance(inner, dict) and "data" in inner:
        inner = inner["data"]

    item_list = None
    if isinstance(inner, dict):
        for key in ("objs", "items", "list", "data", "results",
                     "object_list", "search_list", "feed_list",
                     "video_list"):
            val = inner.get(key)
            if isinstance(val, list) and val:
                item_list = val
                break
    elif isinstance(inner, list):
        item_list = inner

    if not item_list:
        logger.info("[视频号API] No items. Keys: %s",
                    list(inner.keys() if isinstance(inner, dict)
                         else data.keys())[:8])
        return []

    results: list[CrawlResult] = []
    for item in item_list:
        r = _parse_wechat_item(item, seen)
        if r:
            results.append(r)
    return results


def _parse_wechat_item(
    item: dict,
    seen: set[str],
) -> Optional[CrawlResult]:
    """Parse a single WeChat Channels search result item."""
    if not isinstance(item, dict):
        return None

    obj = (item.get("object_info")
           or item.get("feed_info")
           or item.get("video_info")
           or item)

    item_id = str(
        obj.get("id", "")
        or obj.get("objectId", "")
        or obj.get("object_id", "")
        or obj.get("feedId", "")
        or obj.get("feed_id", "")
        or item.get("id", "")
        or item.get("objectId", "")
    )
    if not item_id or item_id in seen:
        desc = (obj.get("desc", "") or obj.get("description", "")
                or obj.get("title", ""))
        if desc and desc not in seen:
            item_id = desc[:50]
        else:
            return None
    seen.add(item_id)

    nickname = ""
    author = (obj.get("author")
              or obj.get("contact")
              or obj.get("user_info")
              or item.get("contact")
              or {})
    if isinstance(author, dict):
        nickname = (author.get("nickname", "")
                    or author.get("name", "")
                    or author.get("username", ""))

    content = (
        obj.get("desc", "")
        or obj.get("description", "")
        or obj.get("title", "")
        or item.get("desc", "")
        or item.get("title", "")
        or "(无文案)"
    )

    link = (
        obj.get("share_url", "")
        or obj.get("url", "")
        or item.get("share_url", "")
        or item.get("url", "")
    )
    if not link:
        export_id = (obj.get("export_id", "")
                     or obj.get("exportId", ""))
        if export_id:
            link = (f"https://channels.weixin.qq.com/web/pages/feed/"
                    f"{export_id}")
        else:
            link = ""

    ts = (obj.get("create_time", 0)
          or obj.get("createTime", 0)
          or obj.get("publish_time", 0)
          or item.get("create_time", 0))
    pub_date = ""
    if ts:
        try:
            from datetime import datetime
            pub_date = datetime.fromtimestamp(int(ts)).strftime(
                "%Y-%m-%d %H:%M")
        except Exception:
            pass

    return CrawlResult(
        platform="微信视频号",
        item_id=item_id,
        nickname=nickname,
        content=content[:500],
        link=link,
        publish_date=pub_date,
    )


async def test_wechat_api(
    token: str,
    city: str = "北京",
    api_base: str = "",
) -> tuple[bool, str]:
    """Quick test for WeChat Channels API access."""
    base = (api_base.strip().rstrip("/") if api_base else "") or DEFAULT_API_BASE
    headers = {"Authorization": f"Bearer {token}"}
    keyword = f"{city}同城"
    params = {"keyword": keyword, "page": 0, "page_size": 3}

    try:
        async with httpx.AsyncClient(
            timeout=15, verify=False,
        ) as client:
            resp = await client.get(
                f"{base}/api/v1/wechat_channels/fetch_search_ordinary",
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
        return False, f"Token 无效: {_detail_msg(detail)}"
    if detail_code == 402 or top_code == 402:
        return False, ("视频号接口需要付费余额（$0.01/次），"
                       "免费额度不适用。请在 TikHub 充值后使用。")

    seen: set[str] = set()
    items = _parse_search_response(data, seen)
    if items:
        return True, f"连接成功！返回 {len(items)} 条视频号内容"
    if top_code in (200, 0):
        return True, "连接成功（API正常，但未搜索到相关内容）"
    return False, f"接口返回异常: code={top_code or detail_code}"
