"""
小红书爬虫 — Playwright SSR extraction + network interception

Data collection strategy:
  1. Navigate to xiaohongshu.com/explore with saved cookies
  2. Extract SSR data from page HTML (__INITIAL_SSR_DATA__ / __NEXT_DATA__)
  3. Intercept /api/sns/web/* API responses triggered by scrolling
  4. Combine and deduplicate
"""
import asyncio
import json
import logging
from datetime import datetime

from .base import BaseCrawler, CrawlResult

logger = logging.getLogger(__name__)

EXPLORE_URL = "https://www.xiaohongshu.com/explore"


class XiaohongshuCrawler(BaseCrawler):
    platform_name = "小红书"

    async def fetch_tongcheng(self) -> list[CrawlResult]:
        if not self.bm:
            return []
        try:
            page = await self.bm.get_page(self.platform_name)
        except Exception as exc:
            logger.warning("[小红书] Browser not available: %s", exc)
            return []

        captured: list[dict] = []

        async def on_response(response):
            url = response.url
            if response.status != 200:
                return
            if not any(p in url for p in (
                "/homefeed", "/feed", "/search", "/note",
            )):
                return
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct:
                    body = await response.json()
                    captured.append(body)
                    logger.debug("[小红书] API captured: %s", url[:150])
            except Exception:
                pass

        page.on("response", on_response)
        ssr_items: list[dict] = []

        try:
            await page.goto(
                EXPLORE_URL, wait_until="domcontentloaded", timeout=30_000,
            )
            await asyncio.sleep(4)

            title = await page.title()
            logger.info("[小红书] Page: '%s' @ %s", title, page.url)

            ssr_items = await self._extract_ssr(page)
            if ssr_items:
                logger.info("[小红书] SSR: found %d items", len(ssr_items))

            found_local = False
            for label in ("附近", "同城", "本地"):
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
                            logger.info("[小红书] Clicked '%s' tab via %s", label, sel)
                            found_local = True
                            break
                    except Exception:
                        continue
                if found_local:
                    break

            if not found_local:
                try:
                    clicked = await page.evaluate("""() => {
                        const els = document.querySelectorAll('a, div, span, li');
                        for (const el of els) {
                            const t = el.textContent?.trim();
                            if (t === '附近' || t === '同城' || t === '本地') {
                                el.click();
                                return t;
                            }
                        }
                        return null;
                    }""")
                    if clicked:
                        await asyncio.sleep(3)
                        logger.info("[小红书] Clicked '%s' via JS", clicked)
                    else:
                        logger.info("[小红书] 附近/同城 tab not found, using default feed")
                except Exception:
                    logger.info("[小红书] 附近/同城 tab not found, using default feed")

            for _ in range(5):
                await page.evaluate(
                    "window.scrollTo(0, document.body.scrollHeight)"
                )
                await asyncio.sleep(2)
            await asyncio.sleep(1)

        except Exception as exc:
            logger.warning("[小红书] Page error: %s", exc)
        finally:
            page.remove_listener("response", on_response)

        await self.bm.save_cookies(self.platform_name)

        results: list[CrawlResult] = []
        seen: set[str] = set()

        for item in ssr_items:
            r = self._parse_note(item, seen)
            if r:
                results.append(r)

        for body in captured:
            results.extend(self._parse_response(body, seen))

        logger.info(
            "[小红书] Total: %d items (SSR=%d, API captures=%d)",
            len(results), len(ssr_items), len(captured),
        )
        return results

    # ── SSR extraction ──

    async def _extract_ssr(self, page) -> list[dict]:
        try:
            raw = await page.evaluate("""() => {
                // Prefer script element textContent (most reliable)
                for (const id of ['__NEXT_DATA__', 'RENDER_DATA',
                                   '__INITIAL_SSR_DATA__']) {
                    const el = document.getElementById(id);
                    if (el && el.textContent && el.textContent.length > 10)
                        return el.textContent;
                }
                // Window properties (skip DOM element refs via nodeType check)
                for (const k of ['__INITIAL_SSR_DATA__', '__NEXT_DATA__',
                                  '__INITIAL_STATE__']) {
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
            return self._dig_note_items(data)
        except Exception as exc:
            logger.debug("[小红书] SSR error: %s", exc)
            return []

    def _dig_note_items(self, obj, depth=0) -> list[dict]:
        if depth > 6:
            return []
        if isinstance(obj, list):
            valid = [
                i for i in obj
                if isinstance(i, dict) and (
                    "note_card" in i or "note" in i
                    or ("id" in i and ("title" in i or "desc" in i or "user" in i))
                )
            ]
            if valid:
                return valid
            out: list[dict] = []
            for item in obj:
                if isinstance(item, dict):
                    out.extend(self._dig_note_items(item, depth + 1))
            return out
        if isinstance(obj, dict):
            for key in ("items", "notes", "feeds", "feedList", "noteList",
                        "data", "list"):
                val = obj.get(key)
                if isinstance(val, list) and val:
                    found = self._dig_note_items(val, depth + 1)
                    if found:
                        return found
            out = []
            for val in obj.values():
                if isinstance(val, (dict, list)):
                    out.extend(self._dig_note_items(val, depth + 1))
            return out
        return []

    # ── API response parsing ──

    def _parse_response(self, body: dict, seen: set) -> list[CrawlResult]:
        items: list[CrawlResult] = []
        data = body.get("data", {})

        if isinstance(data, dict):
            feed_items = data.get("items") or data.get("notes") or []
        elif isinstance(data, list):
            feed_items = data
        else:
            feed_items = []

        for entry in feed_items:
            r = self._parse_note(entry, seen)
            if r:
                items.append(r)
        return items

    def _parse_note(self, item: dict, seen: set) -> CrawlResult | None:
        if not isinstance(item, dict):
            return None

        note = item.get("note_card") or item.get("note") or item
        note_id = str(
            item.get("id", "")
            or note.get("note_id", "")
            or note.get("noteId", "")
            or note.get("id", "")
        )
        if not note_id or note_id in seen:
            return None
        seen.add(note_id)

        user = note.get("user") or item.get("user") or {}
        nickname = user.get("nickname", "") or user.get("name", "")
        user_id = user.get("user_id", "") or user.get("userId", "")

        title = (
            note.get("display_title", "")
            or note.get("title", "")
            or item.get("display_title", "")
        )
        desc = note.get("desc", "") or note.get("description", "")
        content = f"{title} {desc}".strip() if desc else title
        if not content and not nickname:
            return None

        link = f"https://www.xiaohongshu.com/explore/{note_id}"

        ts = (
            note.get("time", 0)
            or note.get("create_time", 0)
            or note.get("last_update_time", 0)
            or note.get("timestamp", 0)
            or item.get("time", 0)
            or item.get("timestamp", 0)
        )
        pub_date = self._ts_to_str(ts) or datetime.now().strftime("%Y-%m-%d %H:%M")

        return CrawlResult(
            platform="小红书",
            item_id=note_id,
            nickname=nickname or str(user_id),
            content=content[:500],
            link=link,
            publish_date=pub_date,
        )

    @staticmethod
    def _ts_to_str(ts) -> str:
        if isinstance(ts, str):
            return ts[:16]
        if not ts:
            return ""
        try:
            if ts > 1e12:
                ts = ts / 1000
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return ""
