"""
爬虫管理器 - 多平台并行，互不干扰
"""
import asyncio
from pathlib import Path
from .base import CrawlResult
from .douyin import DouyinCrawler
from .kuaishou import KuaishouCrawler
from .xiaohongshu import XiaohongshuCrawler
from .wechat import WechatCrawler
from .. import database


CRAWLERS = {
    "抖音": DouyinCrawler,
    "快手": KuaishouCrawler,
    "小红书": XiaohongshuCrawler,
    "微信视频号": WechatCrawler,
}


class CrawlerManager:
    def __init__(self, db_path: Path, platforms: list[str]):
        self.db_path = db_path
        self.platforms = [p for p in platforms if p in CRAWLERS]
        self._running = False
        self._task: asyncio.Task | None = None

    async def _run_platform(self, name: str) -> tuple[str, int, int]:
        """单平台采集，返回 (平台名, 新增数, 重复数)"""
        crawler = CRAWLERS[name]()
        new_count = 0
        skip_count = 0
        try:
            items = await crawler.fetch_tongcheng()
            for r in items:
                ok = await database.insert_collection(
                    self.db_path,
                    platform=r.platform,
                    item_id=r.item_id,
                    nickname=r.nickname,
                    content=r.content,
                    link=r.link,
                    publish_date=r.publish_date,
                )
                if ok:
                    new_count += 1
                else:
                    skip_count += 1
        except Exception as e:
            # 单平台失败不影响其他
            raise e
        return name, new_count, skip_count

    async def run_once(self) -> dict[str, tuple[int, int]]:
        """执行一轮采集，各平台并行"""
        tasks = [self._run_platform(p) for p in self.platforms]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out = {}
        for i, p in enumerate(self.platforms):
            r = results[i]
            if isinstance(r, Exception):
                out[p] = (0, 0)  # 错误时不计
                continue
            _, new, skip = r
            out[p] = (new, skip)
        return out

    async def run_loop(self, callback=None):
        """循环采集，稳定为主，间隔约 60 秒"""
        self._running = True
        while self._running:
            try:
                stats = await self.run_once()
                if callback:
                    callback(stats)
            except Exception as e:
                if callback:
                    callback({"error": str(e)})
            await asyncio.sleep(60)

    def stop(self):
        self._running = False
