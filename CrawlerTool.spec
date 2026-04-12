# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec — bundles ALL dependencies so user installs nothing.

Key packaging concerns:
  - playwright_stealth has 23 JS evasion scripts in js/ that must be included
  - tkcalendar needs Babel locale data
  - Appium-Python-Client + selenium bundled for mobile emulator strategy
  - All src/ submodules explicitly listed as hidden imports
  - config.example.yaml bundled as a data file
"""

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('config.example.yaml', '.'),
    ],
    hiddenimports=[
        # ── Our application modules ──
        'src',
        'src.app',
        'src.auth',
        'src.database',
        'src.llm',
        'src.notify',
        'src.export_utils',
        'src.crawlers',
        'src.crawlers.base',
        'src.crawlers.manager',
        'src.crawlers.browser_manager',
        'src.crawlers.douyin',
        'src.crawlers.kuaishou',
        'src.crawlers.xiaohongshu',
        'src.crawlers.wechat',
        'src.crawlers.appium_douyin',

        # ── Playwright ──
        'playwright',
        'playwright.async_api',
        'playwright.sync_api',
        'playwright._impl',
        'playwright._impl._api_structures',
        'playwright._impl._connection',
        'playwright._impl._browser',
        'playwright._impl._browser_context',
        'playwright._impl._browser_type',
        'playwright._impl._page',
        'playwright._impl._transport',
        'playwright._impl._driver',
        'greenlet',

        # ── Anti-detection ──
        'playwright_stealth',
        'playwright_stealth.stealth',
        'playwright_stealth.context_managers',
        'playwright_stealth.case_insensitive_dict',

        # ── Appium + Selenium (for Android emulator) ──
        'appium',
        'appium.webdriver',
        'appium.options',
        'appium.options.android',
        'appium.options.android.uiautomator2',
        'selenium',
        'selenium.webdriver',
        'selenium.webdriver.common',
        'selenium.webdriver.remote',
        'selenium.webdriver.remote.webdriver',

        # ── Database ──
        'aiosqlite',
        'sqlite3',

        # ── HTTP / Network ──
        'httpx',
        'httpx._transports',
        'aiohttp',
        'requests',
        'urllib3',
        'certifi',
        'charset_normalizer',
        'idna',

        # ── OpenAI SDK ──
        'openai',
        'openai.resources',
        'openai._client',
        'httpcore',
        'anyio',
        'anyio._backends',
        'anyio._backends._asyncio',
        'sniffio',
        'distro',
        'h11',
        'pydantic',
        'pydantic.deprecated',
        'pydantic.deprecated.decorator',
        'pydantic_core',
        'annotated_types',

        # ── Excel ──
        'openpyxl',
        'openpyxl.cell',
        'openpyxl.workbook',
        'pandas',
        'pandas.io.excel',
        'pandas.io.excel._openpyxl',

        # ── Calendar widget ──
        'tkcalendar',
        'babel',
        'babel.core',
        'babel.dates',
        'babel.numbers',

        # ── Config ──
        'yaml',

        # ── Standard library that PyInstaller sometimes misses ──
        'asyncio',
        'json',
        'hashlib',
        'logging',
        'threading',
        'tkinter',
        'tkinter.ttk',
        'tkinter.messagebox',
        'tkinter.filedialog',
        'webbrowser',
        'email.mime.text',
        'email.mime.multipart',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib',
        'numpy.testing',
        'scipy',
        'IPython',
        'jupyter',
        'pytest',
        'unittest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# ── Collect all data files from packages that have non-Python assets ──
from PyInstaller.utils.hooks import (
    collect_data_files, collect_submodules
)

# playwright-stealth JS evasion scripts (critical!)
a.datas += collect_data_files('playwright_stealth')

# tkcalendar themes and locale data
a.datas += collect_data_files('tkcalendar')

# Babel locale data (required by tkcalendar for date formatting)
a.datas += collect_data_files('babel')

# Playwright driver (node binary + scripts)
a.datas += collect_data_files('playwright')

# Certifi CA bundle (for HTTPS requests)
a.datas += collect_data_files('certifi')

# Pydantic (used by OpenAI SDK, may have compiled validators)
try:
    a.datas += collect_data_files('pydantic')
    a.datas += collect_data_files('pydantic_core')
except Exception:
    pass

# Collect all submodules for packages with dynamic imports
a.hiddenimports += collect_submodules('playwright')
a.hiddenimports += collect_submodules('playwright_stealth')
a.hiddenimports += collect_submodules('openai')
try:
    a.hiddenimports += collect_submodules('appium')
    a.hiddenimports += collect_submodules('selenium')
except Exception:
    pass

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='CrawlerTool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
