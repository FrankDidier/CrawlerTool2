"""
小红书同城爬虫
"""
import asyncio
import random
from .base import BaseCrawler, CrawlResult


class XiaohongshuCrawler(BaseCrawler):
    platform_name = "小红书"

    async def fetch_tongcheng(self) -> list[CrawlResult]:
        await asyncio.sleep(random.uniform(1, 3))
        return []
