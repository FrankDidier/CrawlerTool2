"""
Playwright browser management with cascading anti-detection strategies.

Handles browser lifecycle, cookie persistence, interactive login,
and multi-strategy anti-detection for platforms that block automation.

Browser resolution order (for standard launch):
  1. System Google Chrome  (channel="chrome")
  2. System Microsoft Edge (channel="msedge")
  3. Chrome/Edge via explicit executable path (Win10 fallback)
  4. Playwright bundled Chromium
  5. CDP fallback: launch Chrome/Edge via subprocess + connect_over_cdp

Anti-detection strategies (cascading, for search/CAPTCHA-protected pages):
  S1. Standard browser + playwright-stealth patches
  S2. Persistent browser profile + stealth (accumulated state)
  S3. Persistent profile + Chrome extension capture (headed mode)
  S4. CDP with user's real Chrome profile (most "human")
  S5. Main feed fallback (always works, no city-specific content)
"""
import json
import asyncio
import logging
import os
import random
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


# ═══════════════════════════════════════════════════════════
#  Anti-detection: Stealth patches
# ═══════════════════════════════════════════════════════════

async def apply_stealth(page):
    """Apply anti-detection patches to a Playwright page.

    Uses playwright-stealth if installed, plus manual patches as baseline.
    """
    stealth_ok = False
    try:
        from playwright_stealth import Stealth
        s = Stealth()
        await s.apply_stealth_async(page)
        stealth_ok = True
        logger.info("playwright-stealth patches applied")
    except ImportError:
        logger.debug("playwright-stealth not installed, using manual patches")
    except Exception as exc:
        logger.debug("stealth apply error: %s, using manual patches", exc)

    await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5]
        });
        Object.defineProperty(navigator, 'languages', {
            get: () => ['zh-CN', 'zh', 'en-US', 'en']
        });
        if (!window.chrome) window.chrome = {};
        if (!window.chrome.runtime) window.chrome.runtime = {};
        const _origPermQ = window.navigator.permissions
            && window.navigator.permissions.query;
        if (_origPermQ) {
            window.navigator.permissions.query = (params) => (
                params.name === 'notifications'
                    ? Promise.resolve({state: Notification.permission})
                    : _origPermQ.call(window.navigator.permissions, params)
            );
        }
    """)
    return stealth_ok


# ═══════════════════════════════════════════════════════════
#  Human-like behavior utilities
# ═══════════════════════════════════════════════════════════

_USER_AGENTS = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
     "AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/120.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
     "AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/121.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
     "AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/122.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
     "AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/120.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
     "AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"),
]


_MOBILE_DEVICE = {
    "user_agent": ("Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Mobile Safari/537.36"),
    "viewport": {"width": 393, "height": 851},
    "device_scale_factor": 2.75,
    "is_mobile": True,
    "has_touch": True,
}


def random_ua() -> str:
    """Pick a random realistic User-Agent string."""
    return random.choice(_USER_AGENTS)


def random_viewport() -> dict:
    """Generate a slightly randomized viewport size."""
    w = random.choice([1280, 1366, 1440, 1536, 1600, 1920])
    h = random.choice([720, 768, 800, 864, 900, 1080])
    return {"width": w, "height": h}


async def human_scroll(page, times: int = 5, *, jitter: bool = True):
    """Scroll the page like a human — variable speed and distance."""
    for _ in range(times):
        distance = random.randint(300, 900)
        await page.evaluate(
            f"window.scrollBy(0, {distance})")
        delay = random.uniform(1.5, 4.0) if jitter else 2.0
        await asyncio.sleep(delay)


async def human_delay(min_s: float = 1.0, max_s: float = 3.0):
    """Wait a random human-like duration."""
    await asyncio.sleep(random.uniform(min_s, max_s))


async def warm_up_page(page, url: str):
    """Visit the target domain's homepage first to build cookies
    before navigating to the actual target URL."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    home = f"{parsed.scheme}://{parsed.netloc}"
    try:
        await page.goto(home, wait_until="domcontentloaded", timeout=15_000)
        await human_delay(2.0, 4.0)
        await human_scroll(page, times=2)
        await human_delay(1.0, 2.0)
    except Exception as exc:
        logger.debug("Warm-up navigation error: %s", exc)


# ═══════════════════════════════════════════════════════════
#  Embedded Chrome Extension for enhanced data capture
# ═══════════════════════════════════════════════════════════

_EXT_MANIFEST = json.dumps({
    "manifest_version": 3,
    "name": "Data Capture Helper",
    "version": "1.0",
    "content_scripts": [{
        "matches": [
            "*://*.douyin.com/*",
            "*://*.kuaishou.cn/*",
            "*://*.xiaohongshu.com/*",
            "*://channels.weixin.qq.com/*"
        ],
        "js": ["content.js"],
        "run_at": "document_start"
    }],
    "web_accessible_resources": [{
        "resources": ["inject.js"],
        "matches": ["<all_urls>"]
    }]
}, ensure_ascii=False, indent=2)

_EXT_CONTENT_JS = """\
var s = document.createElement('script');
s.src = chrome.runtime.getURL('inject.js');
s.onload = function() { s.remove(); };
(document.head || document.documentElement).appendChild(s);
"""

_EXT_INJECT_JS = """\
(function() {
    if (window.__CRAWLER_HOOK__) return;
    window.__CRAWLER_HOOK__ = true;
    window.__CRAWLER_CAPTURED__ = [];

    var PATTERNS = ['/aweme/', '/graphql', '/api/sns/', '/feed',
                    '/search/', '/nearby/', '/samecity', '/feedlist'];

    function shouldCapture(url) {
        for (var i = 0; i < PATTERNS.length; i++) {
            if (url.indexOf(PATTERNS[i]) !== -1) return true;
        }
        return false;
    }

    var _fetch = window.fetch;
    window.fetch = function() {
        var args = arguments;
        return _fetch.apply(this, args).then(function(resp) {
            try {
                var url = (typeof args[0] === 'string')
                    ? args[0] : (args[0] && args[0].url) || '';
                if (shouldCapture(url)) {
                    resp.clone().json().then(function(data) {
                        window.__CRAWLER_CAPTURED__.push(
                            {url: url, data: data, ts: Date.now()});
                    }).catch(function(){});
                }
            } catch(e) {}
            return resp;
        });
    };

    var _xhrOpen = XMLHttpRequest.prototype.open;
    var _xhrSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(m, url) {
        this.__crawlerUrl = url;
        return _xhrOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function() {
        var self = this;
        this.addEventListener('load', function() {
            try {
                var url = self.__crawlerUrl || '';
                if (shouldCapture(url)) {
                    var data = JSON.parse(self.responseText);
                    window.__CRAWLER_CAPTURED__.push(
                        {url: url, data: data, ts: Date.now()});
                }
            } catch(e) {}
        });
        return _xhrSend.apply(this, arguments);
    };
})();
"""


def _prepare_extension(data_dir: Path) -> Path:
    """Write the embedded Chrome extension to disk. Returns its directory."""
    ext_dir = data_dir / "chrome_extension"
    ext_dir.mkdir(parents=True, exist_ok=True)
    (ext_dir / "manifest.json").write_text(_EXT_MANIFEST, encoding="utf-8")
    (ext_dir / "content.js").write_text(_EXT_CONTENT_JS, encoding="utf-8")
    (ext_dir / "inject.js").write_text(_EXT_INJECT_JS, encoding="utf-8")
    return ext_dir


# ═══════════════════════════════════════════════════════════
#  System browser discovery
# ═══════════════════════════════════════════════════════════

def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _find_browser_executables() -> list:
    """Find Chrome/Edge on the current system (Windows, macOS, Linux)."""
    found = []

    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        prog = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        prog86 = os.environ.get("PROGRAMFILES(X86)",
                                r"C:\Program Files (x86)")
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
                    (r"SOFTWARE\Microsoft\Windows\CurrentVersion"
                     r"\App Paths\chrome.exe", "Chrome (Registry)"),
                    (r"SOFTWARE\Microsoft\Windows\CurrentVersion"
                     r"\App Paths\msedge.exe", "Edge (Registry)"),
                ]:
                    for hive in (winreg.HKEY_LOCAL_MACHINE,
                                 winreg.HKEY_CURRENT_USER):
                        try:
                            with winreg.OpenKey(hive, sub) as key:
                                val = winreg.QueryValue(key, "")
                                if val and os.path.isfile(val):
                                    found.append((val, lbl))
                        except OSError:
                            pass
            except ImportError:
                pass

    elif sys.platform == "darwin":
        candidates = [
            ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
             "Chrome"),
            ("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
             "Edge"),
            (os.path.expanduser(
                "~/Applications/Google Chrome.app"
                "/Contents/MacOS/Google Chrome"), "Chrome (User)"),
        ]
        for path, label in candidates:
            if os.path.isfile(path):
                found.append((path, label))

    else:
        import shutil
        for cmd, label in [
            ("google-chrome", "Chrome"),
            ("chromium-browser", "Chromium"),
            ("microsoft-edge", "Edge"),
        ]:
            p = shutil.which(cmd)
            if p:
                found.append((p, label))

    for path, label in found:
        logger.debug("Found browser: %s at %s", label, path)
    return found


def _find_chrome_user_data_dir():
    """Find the default Chrome user data directory on this system."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            p = os.path.join(local, "Google", "Chrome", "User Data")
            if os.path.isdir(p):
                return p
    elif sys.platform == "darwin":
        p = os.path.expanduser(
            "~/Library/Application Support/Google/Chrome")
        if os.path.isdir(p):
            return p
    else:
        for name in ("google-chrome", "chromium"):
            p = os.path.expanduser(f"~/.config/{name}")
            if os.path.isdir(p):
                return p
    return None


# ═══════════════════════════════════════════════════════════
#  Browser launch helpers
# ═══════════════════════════════════════════════════════════

async def _launch_with_fallback(pw, *, headless: bool):
    """Try channel -> explicit path -> bundled Chromium.  Returns Browser."""
    errors = []

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
    """Launch Chrome/Edge via subprocess, connect Playwright over CDP."""
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


# ═══════════════════════════════════════════════════════════
#  BrowserManager
# ═══════════════════════════════════════════════════════════

class BrowserManager:
    """
    Manages a shared Playwright Chromium browser for all crawlers.
    Each platform gets its own BrowserContext with persistent cookies.

    Additional methods support anti-detection strategies:
      - create_stealth_page()       → S1
      - create_persistent_context() → S2 / S3
      - create_cdp_user_page()      → S4
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

    # ── Standard browser lifecycle ──

    async def _ensure_pw(self):
        if self._pw is None:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()

    async def start(self):
        """Initialize Playwright and launch headless browser."""
        await self._ensure_pw()

        if self._browser is None or not self._browser.is_connected():
            try:
                self._browser = await _launch_with_fallback(
                    self._pw, headless=True)
            except Exception as exc:
                logger.warning("Standard launch failed, trying CDP: %s", exc)
                self._browser, self._cdp_proc = await _launch_via_cdp(
                    self._pw, headless=True)

    async def get_page(self, platform: str):
        """Get or create a page for *platform* with stealth, reused across
        crawl cycles."""
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
        await apply_stealth(page)
        self._pages[platform] = page
        return page

    async def _get_context(self, platform: str):
        """Get or create a BrowserContext, loading saved cookies."""
        if platform in self._contexts:
            return self._contexts[platform]

        if self._browser is None:
            await self.start()

        context = await self._browser.new_context(
            user_agent=random_ua(),
            viewport=random_viewport(),
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

    # ── Strategy S1: Stealth page from existing browser ──

    async def create_stealth_page(self, platform: str):
        """Create a new page with full stealth patches.

        Uses the existing browser & context. Caller must close the page
        when done (page.close()).
        """
        if not self.is_ready:
            await self.start()
        ctx = await self._get_context(platform)
        page = await ctx.new_page()
        await apply_stealth(page)
        return page

    # ── Geolocation context (for 同城 content) ──

    async def create_geo_context(self, platform: str,
                                  lat: float, lng: float, *,
                                  mobile: bool = False):
        """Create a context with geolocation spoofing on the shared browser.

        When mobile=True, emulates a mobile device (viewport, touch, UA).
        Returns (context, page).
        """
        if not self.is_ready:
            await self.start()

        kwargs = {
            "locale": "zh-CN",
            "geolocation": {"latitude": lat, "longitude": lng},
            "permissions": ["geolocation"],
        }
        if mobile:
            kwargs.update(_MOBILE_DEVICE)
        else:
            kwargs["user_agent"] = random_ua()
            kwargs["viewport"] = random_viewport()

        context = await self._browser.new_context(**kwargs)

        cookie_file = self._cookie_path(platform)
        if cookie_file.exists():
            try:
                cookies = json.loads(
                    cookie_file.read_text(encoding="utf-8"))
                if cookies:
                    await context.add_cookies(cookies)
            except Exception:
                pass

        page = await context.new_page()
        await apply_stealth(page)
        return context, page

    # ── Strategy S2/S3: Persistent browser context ──

    async def create_persistent_context(self, platform: str, *,
                                         headless: bool = True,
                                         extension_path: str = None,
                                         geo_coords: tuple = None,
                                         mobile: bool = False):
        """Create a persistent browser context that accumulates state.

        Returns (context, page). Caller must close context when done.
        If extension_path is given, loads the Chrome extension (forces
        headed mode since extensions need a visible browser).
        """
        await self._ensure_pw()

        profile_dir = (self.data_dir / "profiles"
                       / platform.replace("/", "_").replace("\\", "_"))
        profile_dir.mkdir(parents=True, exist_ok=True)

        args = list(LAUNCH_ARGS)
        if extension_path:
            args.extend([
                f'--disable-extensions-except={extension_path}',
                f'--load-extension={extension_path}',
            ])
            headless = False

        ctx_kwargs: dict = {"locale": "zh-CN"}
        if mobile:
            ctx_kwargs.update(_MOBILE_DEVICE)
        else:
            ctx_kwargs["user_agent"] = UA
            ctx_kwargs["viewport"] = {"width": 1280, "height": 720}
        if geo_coords:
            ctx_kwargs["geolocation"] = {
                "latitude": geo_coords[0], "longitude": geo_coords[1]}
            ctx_kwargs["permissions"] = ["geolocation"]

        cookie_file = self._cookie_path(platform)

        ctx = None
        errors = []

        for channel, label in _CHANNELS:
            try:
                ctx = await self._pw.chromium.launch_persistent_context(
                    str(profile_dir),
                    headless=headless,
                    channel=channel,
                    args=args,
                    **ctx_kwargs,
                )
                logger.info("Persistent context via %s (headless=%s)",
                            label, headless)
                break
            except Exception as exc:
                errors.append(f"{label}: {exc}")
                logger.debug("Persistent %s failed: %s", label, exc)

        if ctx is None:
            for exe_path, label in _find_browser_executables():
                try:
                    ctx = await self._pw.chromium.launch_persistent_context(
                        str(profile_dir),
                        headless=headless,
                        executable_path=exe_path,
                        args=args,
                        **ctx_kwargs,
                    )
                    logger.info("Persistent context via %s path", label)
                    break
                except Exception as exc:
                    errors.append(f"{label}: {exc}")

        if ctx is None:
            try:
                ctx = await self._pw.chromium.launch_persistent_context(
                    str(profile_dir),
                    headless=headless,
                    args=args,
                    **ctx_kwargs,
                )
                logger.info("Persistent context via bundled Chromium")
            except Exception as exc:
                errors.append(f"Bundled: {exc}")
                raise RuntimeError(
                    "无法创建持久化浏览器:\n" + "\n".join(errors))

        if cookie_file.exists():
            try:
                cookies = json.loads(
                    cookie_file.read_text(encoding="utf-8"))
                if cookies:
                    await ctx.add_cookies(cookies)
            except Exception:
                pass

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await apply_stealth(page)
        return ctx, page

    # ── Strategy S4: CDP with user's real Chrome profile ──

    async def create_cdp_user_page(self, platform: str):
        """Launch user's actual Chrome with their real profile via CDP.

        Returns (browser, page, subprocess_proc).
        Caller must clean up: browser.close() then proc.terminate().
        """
        await self._ensure_pw()

        user_data = _find_chrome_user_data_dir()
        if not user_data:
            raise RuntimeError(
                "未找到 Chrome 用户数据目录。"
                "请确认已安装 Google Chrome 浏览器。")

        executables = _find_browser_executables()
        if not executables:
            raise RuntimeError("未找到 Chrome/Edge 浏览器可执行文件")

        port = _find_free_port()
        exe_path, label = executables[0]

        cmd = [
            exe_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
        ]

        logger.info("CDP user profile: %s, port %d, data=%s",
                     label, port, user_data)

        creation_flags = 0
        if sys.platform == "win32":
            creation_flags = 0

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )

        cdp_url = f"http://127.0.0.1:{port}"
        browser = None
        for _ in range(20):
            await asyncio.sleep(1)
            try:
                browser = await self._pw.chromium.connect_over_cdp(cdp_url)
                logger.info("CDP user profile connected to %s", label)
                break
            except Exception:
                if proc.poll() is not None:
                    raise RuntimeError(
                        f"{label} 进程已退出 (code={proc.returncode})。"
                        "可能 Chrome 已在运行中，请先关闭所有 Chrome 窗口再试。")

        if browser is None:
            proc.kill()
            raise RuntimeError(
                f"无法连接到 {label} CDP (port {port})。"
                "请关闭所有 Chrome 窗口后重试。")

        ctx = (browser.contexts[0] if browser.contexts
               else await browser.new_context(
                   user_agent=UA,
                   viewport={"width": 1280, "height": 720},
                   locale="zh-CN",
               ))
        page = await ctx.new_page()
        return browser, page, proc

    # ── Chrome extension path ──

    def get_extension_path(self) -> Path:
        """Get path to the embedded Chrome extension (creates on first call)."""
        return _prepare_extension(self.data_dir)

    # ── Interactive login ──

    async def login_interactive(self, platform: str) -> bool:
        """Open a *visible* browser for the user to log in manually."""
        url = PLATFORM_LOGIN_URLS.get(platform)
        if not url:
            return False

        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        saved_cookies: list = []
        cdp_proc = None

        try:
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
            await apply_stealth(page)

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
    def _has_login_markers(platform: str, cookies: list) -> bool:
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
