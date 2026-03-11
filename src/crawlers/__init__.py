"""
爬虫模块 - 4 平台同城数据采集 (Playwright browser scraping)
"""
from .base import BaseCrawler, CrawlResult
from .browser_manager import BrowserManager
from .manager import CrawlerManager

__all__ = ["BaseCrawler", "CrawlResult", "BrowserManager", "CrawlerManager"]
