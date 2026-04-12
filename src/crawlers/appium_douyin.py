"""
抖音移动端模拟器采集 — Appium + Android Emulator

Strategy S5 in the anti-detection cascade: automate the real Douyin
mobile app running on an Android emulator via Appium.

The Douyin mobile app has a prominent 同城 (local city) tab that is
NOT available on the desktop web version. This approach:
  1. Connects to Appium server → Android emulator
  2. Launches Douyin app
  3. Taps the 同城 tab
  4. Scrolls through the feed
  5. Extracts video metadata from UI elements

Prerequisites (user must set up once):
  - Android SDK with an emulator image (API 30+)
  - Appium 2.x server running: `appium`
  - Douyin APK installed in the emulator
  - pip install Appium-Python-Client

This module gracefully returns empty if Appium is not available.
"""
import asyncio
import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

APPIUM_URL = "http://127.0.0.1:4723"

DOUYIN_PACKAGE = "com.ss.android.ugc.aweme"
DOUYIN_ACTIVITY = ".splash.SplashActivity"

_APPIUM_AVAILABLE = False
try:
    from appium import webdriver as appium_webdriver
    from appium.options.android import UiAutomator2Options
    _APPIUM_AVAILABLE = True
except ImportError:
    pass


def is_available() -> bool:
    """Check if Appium Python client is installed."""
    return _APPIUM_AVAILABLE


async def fetch_douyin_tongcheng(target_city: str,
                                  status_callback=None
                                  ) -> list:
    """Run Douyin mobile app automation via Appium.

    Returns list of dicts with keys: item_id, nickname, content, link,
    publish_date — same shape as CrawlResult fields.

    Runs the blocking Appium calls in a thread executor.
    """
    if not _APPIUM_AVAILABLE:
        logger.info("[Appium] Appium-Python-Client not installed, skipping")
        return []

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _run_appium_sync, target_city, status_callback)


def _notify(callback, msg):
    logger.info(msg)
    if callback:
        try:
            callback(msg)
        except Exception:
            pass


def _run_appium_sync(target_city: str, status_callback=None) -> list:
    """Synchronous Appium automation (runs in thread)."""
    from appium import webdriver as appium_wd
    from appium.options.android import UiAutomator2Options

    _notify(status_callback,
            "[抖音] 方案5: 连接 Android 模拟器...")

    options = UiAutomator2Options()
    options.platform_name = "Android"
    options.device_name = "emulator-5554"
    options.app_package = DOUYIN_PACKAGE
    options.app_activity = DOUYIN_ACTIVITY
    options.no_reset = True
    options.auto_grant_permissions = True
    options.new_command_timeout = 120

    driver = None
    try:
        driver = appium_wd.Remote(APPIUM_URL, options=options)
        driver.implicitly_wait(10)

        _notify(status_callback,
                "[抖音] 方案5: 抖音App已启动，正在寻找同城标签...")

        _switch_to_tongcheng(driver, target_city, status_callback)

        _notify(status_callback,
                "[抖音] 方案5: 正在采集同城内容...")

        items = _scrape_feed(driver, scroll_count=8)

        _notify(status_callback,
                f"[抖音] 方案5: 采集到 {len(items)} 条同城数据")

        return items

    except Exception as exc:
        logger.error("[Appium] Douyin automation error: %s", exc)
        _notify(status_callback,
                f"[抖音] 方案5: 模拟器出错 — {exc}")
        return []
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def _switch_to_tongcheng(driver, target_city, callback):
    """Navigate to the 同城 tab in Douyin mobile app."""
    import time

    time.sleep(3)

    _dismiss_popups(driver)

    for text in ("同城", "附近", target_city):
        try:
            tab = driver.find_element(
                "xpath",
                f'//android.widget.TextView[@text="{text}"]')
            tab.click()
            time.sleep(2)
            _notify(callback,
                    f"[抖音] 方案5: 已切换到「{text}」标签")
            return
        except Exception:
            continue

    try:
        tabs = driver.find_elements(
            "xpath",
            '//android.widget.TextView[contains(@resource-id, "tab")]')
        for tab in tabs:
            t = tab.text.strip()
            if t in ("同城", "附近", "本地") or (target_city and target_city in t):
                tab.click()
                time.sleep(2)
                _notify(callback,
                        f"[抖音] 方案5: 已切换到「{t}」")
                return
    except Exception:
        pass

    _notify(callback,
            "[抖音] 方案5: 未找到同城标签，将在当前页面采集")


def _dismiss_popups(driver):
    """Close common Douyin startup popups/dialogs."""
    import time

    dismiss_texts = [
        "我知道了", "同意", "允许", "以后再说", "跳过",
        "关闭", "取消", "暂不", "不感兴趣",
    ]
    for text in dismiss_texts:
        try:
            btn = driver.find_element(
                "xpath",
                f'//android.widget.TextView[@text="{text}"]')
            btn.click()
            time.sleep(0.5)
        except Exception:
            pass

    for text in dismiss_texts:
        try:
            btn = driver.find_element(
                "xpath",
                f'//android.widget.Button[@text="{text}"]')
            btn.click()
            time.sleep(0.5)
        except Exception:
            pass


def _scrape_feed(driver, scroll_count=8) -> list:
    """Scroll through the Douyin feed and extract video metadata."""
    import time

    seen_ids: set = set()
    items: list = []
    screen_size = driver.get_window_size()
    w = screen_size["width"]
    h = screen_size["height"]

    for i in range(scroll_count):
        page_items = _extract_visible_items(driver, seen_ids)
        items.extend(page_items)

        driver.swipe(
            w // 2, int(h * 0.75),
            w // 2, int(h * 0.25),
            duration=800
        )
        time.sleep(2)

    page_items = _extract_visible_items(driver, seen_ids)
    items.extend(page_items)

    return items


def _extract_visible_items(driver, seen_ids: set) -> list:
    """Extract video info from currently visible UI elements."""
    items = []

    try:
        nickname_els = driver.find_elements(
            "xpath",
            '//android.widget.TextView[contains(@resource-id, "nick")]'
            '|//android.widget.TextView[contains(@resource-id, "name")]'
            '|//android.widget.TextView[contains(@resource-id, "author")]')

        desc_els = driver.find_elements(
            "xpath",
            '//android.widget.TextView[contains(@resource-id, "desc")]'
            '|//android.widget.TextView[contains(@resource-id, "title")]'
            '|//android.widget.TextView[contains(@resource-id, "caption")]')

        if nickname_els or desc_els:
            nickname = ""
            content = ""

            for el in nickname_els:
                t = el.text.strip()
                if t and len(t) < 50:
                    nickname = t
                    break

            for el in desc_els:
                t = el.text.strip()
                if t and len(t) > 2:
                    content = t
                    break

            if nickname or content:
                item_id = f"appium_{hash((nickname, content)) & 0xFFFFFFFF:08x}"
                if item_id not in seen_ids:
                    seen_ids.add(item_id)
                    items.append({
                        "item_id": item_id,
                        "nickname": nickname,
                        "content": content[:500],
                        "link": "",
                        "publish_date": datetime.now().strftime(
                            "%Y-%m-%d %H:%M"),
                    })
    except Exception as exc:
        logger.debug("[Appium] Element extraction error: %s", exc)

    try:
        all_texts = driver.find_elements(
            "xpath",
            '//android.widget.TextView')

        texts = []
        for el in all_texts:
            try:
                t = el.text.strip()
                if t and 5 < len(t) < 300:
                    texts.append(t)
            except Exception:
                pass

        for text in texts:
            if _looks_like_video_desc(text):
                item_id = f"appium_{hash(text) & 0xFFFFFFFF:08x}"
                if item_id not in seen_ids:
                    seen_ids.add(item_id)
                    items.append({
                        "item_id": item_id,
                        "nickname": "",
                        "content": text[:500],
                        "link": "",
                        "publish_date": datetime.now().strftime(
                            "%Y-%m-%d %H:%M"),
                    })
    except Exception as exc:
        logger.debug("[Appium] Text fallback error: %s", exc)

    return items


def _looks_like_video_desc(text: str) -> bool:
    """Heuristic: does this text look like a video description?"""
    if len(text) < 8:
        return False
    if text.startswith(("http", "www.", "关注", "粉丝", "获赞")):
        return False
    if re.match(r'^\d+[.:]?\d*[万亿kKmM]?$', text):
        return False
    if text in ("推荐", "关注", "同城", "搜索", "消息", "我"):
        return False
    has_chinese = bool(re.search(r'[\u4e00-\u9fff]', text))
    has_hashtag = '#' in text or '@' in text
    return has_chinese or has_hashtag or len(text) > 20
