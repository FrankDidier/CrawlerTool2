"""
快手同城爬虫
"""
import asyncio
import random
from .base import BaseCrawler, CrawlResult


class KuaishouCrawler(BaseCrawler):
    platform_name = "快手"

    async def fetch_tongcheng(self) -> list[CrawlResult]:
        await asyncio.sleep(random.uniform(1, 3))
        return []
