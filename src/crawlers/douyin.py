"""
抖音同城爬虫
注：抖音同城需移动端接口或 Web 模拟，此处提供框架接口
实际实现需根据当前接口反爬策略调整
"""
import asyncio
import random
from .base import BaseCrawler, CrawlResult


class DouyinCrawler(BaseCrawler):
    platform_name = "抖音"

    async def fetch_tongcheng(self) -> list[CrawlResult]:
        # 占位实现：实际需接入抖音同城 API / 模拟请求
        # 建议：使用 Playwright 模拟移动端，或接入第三方数据服务
        await asyncio.sleep(random.uniform(1, 3))  # 模拟延迟，稳定为主
        return []
        # 返回示例格式：
        # return [
        #     CrawlResult(
        #         platform="抖音",
        #         item_id="xxx",
        #         nickname="用户昵称",
        #         content="发布内容",
        #         link="https://...",
        #         publish_date="2024-01-01 12:00",
        #     )
        # ]
