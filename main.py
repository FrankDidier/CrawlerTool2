#!/usr/bin/env python3
"""
爬虫小工具 - 启动入口
"""
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

from src.app import main

if __name__ == "__main__":
    main()
