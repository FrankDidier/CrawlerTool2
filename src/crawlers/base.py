"""
爬虫基类 - 各平台实现此类
"""
from dataclasses import dataclass
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .browser_manager import BrowserManager


@dataclass
class CrawlResult:
    platform: str
    item_id: str
    nickname: str
    content: str
    link: str
    publish_date: str


class BaseCrawler(ABC):
    """同城数据爬虫基类 — Playwright network-intercept approach"""

    def __init__(self, browser_manager: "BrowserManager | None" = None):
        self.bm = browser_manager
        self.target_city: str = ""

    @property
    @abstractmethod
    def platform_name(self) -> str:
        pass

    @abstractmethod
    async def fetch_tongcheng(self) -> list[CrawlResult]:
        """获取同城频道数据，返回列表"""
        pass
