"""
爬虫基类 - 各平台实现此类
"""
from dataclasses import dataclass
from abc import ABC, abstractmethod


@dataclass
class CrawlResult:
    platform: str
    item_id: str
    nickname: str
    content: str
    link: str
    publish_date: str


class BaseCrawler(ABC):
    """同城数据爬虫基类"""

    def __init__(self, interval_seconds: int = 30):
        self.interval_seconds = interval_seconds  # 采集间隔，稳定为主

    @property
    @abstractmethod
    def platform_name(self) -> str:
        pass

    @abstractmethod
    async def fetch_tongcheng(self) -> list[CrawlResult]:
        """获取同城频道数据，返回列表"""
        pass
