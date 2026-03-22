"""
Playwright browser management for real web scraping.

Handles browser lifecycle, cookie persistence, and interactive login.
Each platform gets its own BrowserContext with saved cookies.

Browser resolution order:
  1. System Google Chrome  (channel="chrome")
  2. System Microsoft Edge (channel="msedge")
  3. Chrome/Edge via explicit executable path (Win10 fallback)
  4. Playwright bundled Chromium
  5. CDP fallback: launch Chrome/Edge via subprocess + connect_over_cdp
"""
import json
import asyncio
import logging
import os
import socket
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

PLATFORM_LOGIN_URLS = {
    "快手": "https://www.kuaishou.cn",
    "抖音": "https://www.douyin.com",
    "小红书": "https://www.xiaohongshu.com",
    "微信视频号": "https://channels.weixin.qq.com",
}

_LOGIN_COOKIE_MARKERS = {
    "抖音": ("sessionid", "passport_csrf_token", "LOGIN_STATUS", "uid_tt",
             "sid_tt", "ssid_ucp_v1"),
    "快手": ("userId", "kuaishou.server.web_st", "did",
             "passToken", "user-id"),
    "小红书": ("web_session", "xsecappid", "a1", "webId",
              "galaxy_creator_session_id"),
    "微信视频号": ("wxuin", "pass_ticket", "uin", "loginInfo"),
}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
]

_CHANNELS = [
    ("chrome", "Google Chrome"),
    ("msedge", "Microsoft Edge"),
]


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _find_browser_executables() -> list[tuple[str, str]]:
    """Find Chrome/Edge on Windows via common paths + registry."""
    if sys.platform != "win32":
        return []

    found: list[tuple[str, str]] = []
    local = os.environ.get("LOCALAPPDATA", "")
    prog = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    prog86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")

    candidates = [
        (os.path.join(prog, r"Google\Chrome\Application\chrome.exe"),
         "Chrome"),
        (os.path.join(prog86, r"Google\Chrome\Application\chrome.exe"),
         "Chrome (x86)"),
        (os.path.join(local, r"Google\Chrome\Application\chrome.exe")
         if local else "", "Chrome (User)"),
        (os.path.join(prog, r"Microsoft\Edge\Application\msedge.exe"),
         "Edge"),
        (os.path.join(prog86, r"Microsoft\Edge\Application\msedge.exe"),
         "Edge (x86)"),
    ]

    for path, label in candidates:
        if path and os.path.isfile(path):
            found.append((path, label))

    if not found:
        try:
            import winreg
            for sub, lbl in [
                (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                 "Chrome (Registry)"),
                (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
                 "Edge (Registry)"),
            ]:
                for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                    try:
                        with winreg.OpenKey(hive, sub) as key:
                            val = winreg.QueryValue(key, "")
                            if val and os.path.isfile(val):
                                found.append((val, lbl))
                    except OSError:
                        pass
        except ImportError:
            pass

    for path, label in found:
        logger.debug("Found browser: %s at %s", label, path)
    return found


async def _launch_with_fallback(pw, *, headless: bool):
    """Try channel → explicit path → bundled Chromium.  Returns Browser."""
    errors: list[str] = []

    for channel, label in _CHANNELS:
        try:
            browser = await pw.chromium.launch(
                headless=headless, channel=channel, args=LAUNCH_ARGS,
            )
            logger.info("Launched %s (channel=%s, headless=%s)",
                        label, channel, headless)
            return browser
        except Exception as exc:
            errors.append(f"{label}: {exc}")
            logger.debug("Cannot launch %s: %s", label, exc)

    for exe_path, label in _find_browser_executables():
        try:
            browser = await pw.chromium.launch(
                headless=headless, executable_path=exe_path, args=LAUNCH_ARGS,
            )
            logger.info("Launched %s via path (headless=%s)", label, headless)
            return browser
        except Exception as exc:
            errors.append(f"{label} ({exe_path}): {exc}")
            logger.debug("Cannot launch %s at %s: %s", label, exe_path, exc)

    try:
        browser = await pw.chromium.launch(headless=headless, args=LAUNCH_ARGS)
        logger.info("Launched Playwright Chromium (headless=%s)", headless)
        return browser
    except Exception as exc:
        errors.append(f"Playwright Chromium: {exc}")

    raise RuntimeError(
        "Playwright launch failed.\n" + "\n".join(errors)
    )


async def _launch_via_cdp(pw, *, headless: bool, start_url: str = ""):
    """Launch Chrome/Edge via subprocess, connect Playwright over CDP.

    This bypasses Playwright's internal launcher entirely — only needs
    the Chrome binary on the system and Playwright's CDP client.
    """
    executables = _find_browser_executables()
    if not executables:
        raise RuntimeError("未找到 Chrome 或 Edge 浏览器可执行文件")

    port = _find_free_port()
    exe_path, label = executables[0]

    cmd = [
        exe_path,
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-blink-features=AutomationControlled",
    ]
    if headless:
        cmd.append("--headless=new")
    if start_url:
        cmd.append(start_url)

    logger.info("CDP launch: %s on port %d (headless=%s)",
                label, port, headless)

    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NO_WINDOW if headless else 0

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )

    cdp_url = f"http://127.0.0.1:{port}"
    for attempt in range(15):
        await asyncio.sleep(1)
        try:
            browser = await pw.chromium.connect_over_cdp(cdp_url)
            logger.info("CDP connected to %s on port %d", label, port)
            return browser, proc
        except Exception:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"{label} 进程已退出 (code={proc.returncode})")

    proc.kill()
    raise RuntimeError(f"无法连接到 {label} CDP (port {port})")


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
        self._cdp_proc = None
        self._contexts: dict = {}
        self._pages: dict = {}
        self._start_error: str = ""

    @property
    def is_ready(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    # ── Browser lifecycle ──

    async def start(self):
        """Initialize Playwright and launch headless browser."""
        from playwright.async_api import async_playwright

        if self._pw is None:
            self._pw = await async_playwright().start()

        if self._browser is None or not self._browser.is_connected():
            try:
                self._browser = await _launch_with_fallback(
                    self._pw, headless=True)
            except Exception as exc:
                logger.warning("Standard launch failed, trying CDP: %s", exc)
                self._browser, self._cdp_proc = await _launch_via_cdp(
                    self._pw, headless=True)

    async def get_page(self, platform: str):
        """Get or create a page for *platform*, reusing across cycles."""
        if not self.is_ready:
            raise RuntimeError(self._start_error or "Browser not started")

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
                    logger.info("[%s] Loaded %d saved cookies",
                                platform, len(cookies))
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
        """Check if saved login cookies exist for *platform*."""
        p = self._cookie_path(platform)
        if not p.exists():
            return False
        try:
            cookies = json.loads(p.read_text(encoding="utf-8"))
            if not cookies:
                return False
            return self._has_login_markers(platform, cookies)
        except Exception:
            return False

    def _cookie_path(self, platform: str) -> Path:
        safe = platform.replace("/", "_").replace("\\", "_")
        return self.data_dir / f"{safe}_cookies.json"

    # ── Interactive login ──

    async def login_interactive(self, platform: str) -> bool:
        """Open a *visible* browser for the user to log in manually.

        Tries Playwright launch first, falls back to CDP subprocess.
        """
        url = PLATFORM_LOGIN_URLS.get(platform)
        if not url:
            return False

        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        saved_cookies: list = []
        cdp_proc = None

        try:
            # Try standard launch first, fall back to CDP
            try:
                browser = await _launch_with_fallback(pw, headless=False)
            except Exception as exc:
                logger.warning("Standard visible launch failed, "
                               "trying CDP: %s", exc)
                browser, cdp_proc = await _launch_via_cdp(
                    pw, headless=False, start_url=url)

            context = await browser.new_context(
                user_agent=UA,
                viewport={"width": 1280, "height": 720},
                locale="zh-CN",
            )
            page = await context.new_page()
            await page.add_init_script(
                "Object.defineProperty(navigator,'webdriver',"
                "{get:()=>undefined})"
            )

            if not cdp_proc:
                await page.goto(url, wait_until="domcontentloaded")

            done = asyncio.Event()
            page.on("close", lambda _: done.set())
            browser.on("disconnected", lambda: done.set())

            while not done.is_set():
                try:
                    cookies = await context.cookies()
                    if cookies:
                        saved_cookies = cookies
                except Exception:
                    break
                try:
                    await asyncio.wait_for(done.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass

            if browser.is_connected():
                try:
                    final = await context.cookies()
                    if final:
                        saved_cookies = final
                except Exception:
                    pass
                try:
                    await browser.close()
                except Exception:
                    pass

        except Exception as exc:
            logger.error("[%s] Login browser error: %s", platform, exc)
        finally:
            if cdp_proc and cdp_proc.poll() is None:
                try:
                    cdp_proc.terminate()
                except Exception:
                    pass
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

            is_real = self._has_login_markers(platform, saved_cookies)
            logger.info("[%s] Saved %d cookies (login=%s)",
                        platform, len(saved_cookies), is_real)
            return is_real
        return False

    # ── Cookie validation ──

    @staticmethod
    def _has_login_markers(platform: str, cookies: list[dict]) -> bool:
        """Check whether *cookies* contain platform-specific login markers."""
        markers = _LOGIN_COOKIE_MARKERS.get(platform, ())
        if not markers:
            return bool(cookies)
        names = {c.get("name", "") for c in cookies}
        return any(m in names for m in markers)

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

        if self._cdp_proc and self._cdp_proc.poll() is None:
            try:
                self._cdp_proc.terminate()
            except Exception:
                pass
            self._cdp_proc = None

        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None
