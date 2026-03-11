"""
微信视频号同城爬虫
"""
import asyncio
import random
from .base import BaseCrawler, CrawlResult


class WechatCrawler(BaseCrawler):
    platform_name = "微信视频号"

    async def fetch_tongcheng(self) -> list[CrawlResult]:
        await asyncio.sleep(random.uniform(1, 3))
        return []
