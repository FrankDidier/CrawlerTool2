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

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# ── Collect data files BEFORE Analysis (returns 2-tuples accepted by datas=) ──
extra_datas = [
    ('config.example.yaml', '.'),
]
extra_datas += collect_data_files('playwright_stealth')
extra_datas += collect_data_files('tkcalendar')
extra_datas += collect_data_files('babel')
extra_datas += collect_data_files('playwright')
extra_datas += collect_data_files('certifi')
try:
    extra_datas += collect_data_files('pydantic')
    extra_datas += collect_data_files('pydantic_core')
except Exception:
    pass

# ── Collect submodules BEFORE Analysis ──
extra_hiddenimports = []
extra_hiddenimports += collect_submodules('playwright')
extra_hiddenimports += collect_submodules('playwright_stealth')
extra_hiddenimports += collect_submodules('openai')
try:
    extra_hiddenimports += collect_submodules('appium')
    extra_hiddenimports += collect_submodules('selenium')
except Exception:
    pass

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=extra_datas,
    hiddenimports=extra_hiddenimports + [
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
