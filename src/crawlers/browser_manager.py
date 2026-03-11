"""
Playwright browser management for real web scraping.

Handles browser lifecycle, cookie persistence, and interactive login.
Each platform gets its own BrowserContext with saved cookies.
"""
import json
import asyncio
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

PLATFORM_LOGIN_URLS = {
    "快手": "https://www.kuaishou.cn",
    "抖音": "https://www.douyin.com",
    "小红书": "https://www.xiaohongshu.com",
    "微信视频号": "https://channels.weixin.qq.com",
}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


async def install_playwright_browsers():
    """Install Playwright Chromium browser binaries if missing."""
    import subprocess

    def _install():
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to install Playwright browsers:\n{result.stderr}"
            )

    await asyncio.to_thread(_install)
    logger.info("Playwright Chromium installed successfully")


class BrowserManager:
    """
    Manages a shared Playwright Chromium browser for all crawlers.
    Each platform gets its own BrowserContext with persistent cookies.
    """

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir / "browser"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._pw = None
        self._browser = None
        self._contexts: dict = {}
        self._pages: dict = {}
        self._start_error: str = ""

    @property
    def is_ready(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    # ── Browser lifecycle ──

    async def start(self):
        """Initialize Playwright and launch headless Chromium."""
        from playwright.async_api import async_playwright

        if self._pw is None:
            self._pw = await async_playwright().start()

        if self._browser is None or not self._browser.is_connected():
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
            try:
                self._browser = await self._pw.chromium.launch(
                    headless=True, args=launch_args,
                )
            except Exception as exc:
                if "Executable doesn't exist" in str(exc):
                    logger.info("Chromium not found — installing via Playwright …")
                    await install_playwright_browsers()
                    self._browser = await self._pw.chromium.launch(
                        headless=True, args=launch_args,
                    )
                else:
                    raise

    async def get_page(self, platform: str):
        """Get or create a page for *platform*, reusing across cycles."""
        if not self.is_ready:
            raise RuntimeError(
                self._start_error or "Browser not started"
            )

        if platform in self._pages:
            page = self._pages[platform]
            if not page.is_closed():
                try:
                    await page.evaluate("1+1")
                    return page
                except Exception:
                    pass

        ctx = await self._get_context(platform)
        page = await ctx.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        self._pages[platform] = page
        return page

    async def _get_context(self, platform: str):
        """Get or create a BrowserContext, loading saved cookies."""
        if platform in self._contexts:
            return self._contexts[platform]

        if self._browser is None:
            await self.start()

        context = await self._browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 720},
            locale="zh-CN",
        )

        cookie_file = self._cookie_path(platform)
        if cookie_file.exists():
            try:
                cookies = json.loads(cookie_file.read_text(encoding="utf-8"))
                if cookies:
                    await context.add_cookies(cookies)
                    logger.info("[%s] Loaded %d saved cookies", platform, len(cookies))
            except Exception as exc:
                logger.warning("[%s] Cookie load failed: %s", platform, exc)

        self._contexts[platform] = context
        return context

    async def save_cookies(self, platform: str):
        """Persist current cookies to disk."""
        if platform not in self._contexts:
            return
        try:
            cookies = await self._contexts[platform].cookies()
            self._cookie_path(platform).write_text(
                json.dumps(cookies, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("[%s] Cookie save failed: %s", platform, exc)

    def has_cookies(self, platform: str) -> bool:
        """Check if saved cookies exist for *platform*."""
        p = self._cookie_path(platform)
        if not p.exists():
            return False
        try:
            return bool(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            return False

    def _cookie_path(self, platform: str) -> Path:
        safe = platform.replace("/", "_").replace("\\", "_")
        return self.data_dir / f"{safe}_cookies.json"

    # ── Interactive login ──

    async def login_interactive(self, platform: str) -> bool:
        """
        Open a *visible* browser for the user to log in manually.
        Cookies are captured periodically while the browser is open.
        The user closes the browser window when done.
        Returns True if cookies were obtained.
        """
        url = PLATFORM_LOGIN_URLS.get(platform)
        if not url:
            return False

        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        saved_cookies: list = []
        try:
            browser = await pw.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            context = await browser.new_context(
                user_agent=UA,
                viewport={"width": 1280, "height": 720},
                locale="zh-CN",
            )
            page = await context.new_page()
            await page.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )
            await page.goto(url, wait_until="domcontentloaded")

            while browser.is_connected():
                try:
                    cookies = await context.cookies()
                    if cookies:
                        saved_cookies = cookies
                except Exception:
                    break
                await asyncio.sleep(2)

        except Exception as exc:
            logger.error("[%s] Login browser error: %s", platform, exc)
        finally:
            try:
                await pw.stop()
            except Exception:
                pass

        if saved_cookies:
            self._cookie_path(platform).write_text(
                json.dumps(saved_cookies, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if platform in self._contexts:
                try:
                    await self._contexts[platform].close()
                except Exception:
                    pass
                del self._contexts[platform]
            self._pages.pop(platform, None)
            logger.info("[%s] Saved %d cookies from login", platform, len(saved_cookies))
            return True
        return False

    # ── Cleanup ──

    async def close(self):
        """Release all browser resources."""
        for p in list(self._pages.values()):
            try:
                if not p.is_closed():
                    await p.close()
            except Exception:
                pass
        self._pages.clear()

        for c in list(self._contexts.values()):
            try:
                await c.close()
            except Exception:
                pass
        self._contexts.clear()

        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None

        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None
