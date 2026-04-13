#!/usr/bin/env python3
"""
爬虫小工具 - 启动入口
"""
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _get_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


LOG_DIR = _get_root() / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "crawler.log"

_fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")

_console = logging.StreamHandler()
_console.setLevel(logging.INFO)
_console.setFormatter(_fmt)

_file = RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8",
)
_file.setLevel(logging.DEBUG)
_file.setFormatter(_fmt)

logging.basicConfig(level=logging.DEBUG, handlers=[_console, _file])

logging.getLogger(__name__).info(
    "日志文件: %s  (最大 5MB × 5 份轮转)", LOG_FILE)

from src.app import main  # noqa: E402

if __name__ == "__main__":
    main()
