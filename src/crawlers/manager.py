"""
爬虫管理器 — 多平台并行 Playwright 采集

Creates a shared BrowserManager, instantiates per-platform crawlers,
and runs a continuous crawl loop with automatic browser cleanup.
"""
import asyncio
import logging
from pathlib import Path
from .browser_manager import BrowserManager
from .douyin import DouyinCrawler
from .kuaishou import KuaishouCrawler
from .xiaohongshu import XiaohongshuCrawler
from .wechat import WechatCrawler
from .. import database

logger = logging.getLogger(__name__)

CRAWLERS = {
    "抖音": DouyinCrawler,
    "快手": KuaishouCrawler,
    "小红书": XiaohongshuCrawler,
    "微信视频号": WechatCrawler,
}


class CrawlerManager:
    def __init__(self, db_path: Path, platforms: list[str],
                 data_dir=None, *,
                 target_city: str = "",
                 status_callback=None):
        self.db_path = db_path
        self.platforms = [p for p in platforms if p in CRAWLERS]
        self.target_city = target_city
        self.status_callback = status_callback
        self._running = False

        if data_dir is None:
            data_dir = db_path.parent
        self.bm = BrowserManager(data_dir)
        self._crawlers: dict = {}

    async def _ensure_crawlers(self):
        """Initialize browser and create crawler instances (once)."""
        if not self._crawlers:
            try:
                await self.bm.start()
            except Exception as exc:
                self.bm._start_error = str(exc)
                logger.error("Browser start failed: %s — crawlers will return empty", exc)
            for name in self.platforms:
                crawler = CRAWLERS[name](self.bm)
                crawler.target_city = self.target_city
                crawler.status_callback = self.status_callback
                self._crawlers[name] = crawler

    async def _run_platform(self, name: str) -> tuple[str, int, int]:
        """Single-platform crawl; returns (name, new_count, dup_count)."""
        crawler = self._crawlers[name]
        new_count = dup_count = 0
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
                    dup_count += 1
        except Exception as exc:
            logger.error("[%s] Crawl error: %s", name, exc)
            raise
        return name, new_count, dup_count

    async def run_once(self) -> dict[str, tuple[int, int]]:
        """Execute one round of crawling across all selected platforms."""
        await self._ensure_crawlers()
        tasks = [self._run_platform(p) for p in self.platforms]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: dict[str, tuple[int, int]] = {}
        for i, p in enumerate(self.platforms):
            r = results[i]
            if isinstance(r, Exception):
                logger.error("[%s] Error: %s", p, r)
                out[p] = (0, 0)
            else:
                _, new, dup = r
                out[p] = (new, dup)
        return out

    async def run_loop(self, callback=None):
        """Continuous crawl loop — 60 s between rounds, auto-cleanup."""
        self._running = True
        try:
            while self._running:
                try:
                    stats = await self.run_once()
                    if callback:
                        callback(stats)
                except Exception as exc:
                    logger.error("Crawl loop error: %s", exc)
                    if callback:
                        callback({"error": str(exc)})
                # Interruptible 60-second wait
                for _ in range(30):
                    if not self._running:
                        break
                    await asyncio.sleep(2)
        finally:
            await self.cleanup()

    async def cleanup(self):
        """Release all browser resources."""
        self._crawlers.clear()
        try:
            await self.bm.close()
        except Exception:
            pass

    def stop(self):
        self._running = False
