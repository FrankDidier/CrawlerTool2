"""
爬虫管理器 — 多平台顺序 Playwright 采集

Creates a shared BrowserManager, instantiates per-platform crawlers,
and runs a continuous crawl loop. Platforms run sequentially to avoid
shared-browser conflicts and confusing interleaved status messages.
"""
from __future__ import annotations

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
                 status_callback=None,
                 douyin_api_config: dict | None = None):
        self.db_path = db_path
        self.platforms = [p for p in platforms if p in CRAWLERS]
        self.target_city = target_city
        self.status_callback = status_callback
        self.douyin_api_config = douyin_api_config or {}
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
                logger.error("Browser start failed: %s — crawlers will "
                             "return empty", exc)
            for name in self.platforms:
                crawler = CRAWLERS[name](self.bm)
                crawler.target_city = self.target_city
                crawler.status_callback = self.status_callback
                if name in ("抖音", "微信视频号") and self.douyin_api_config:
                    crawler.api_config = self.douyin_api_config
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
        """Execute one round — platforms run sequentially to avoid
        shared-browser conflicts and confusing interleaved messages."""
        await self._ensure_crawlers()
        out: dict[str, tuple[int, int]] = {}
        for p in self.platforms:
            if not self._running:
                break
            try:
                name, new, dup = await self._run_platform(p)
                out[p] = (new, dup)
            except Exception as exc:
                logger.error("[%s] Error: %s", p, exc)
                out[p] = (0, 0)
        return out

    async def run_loop(self, callback=None):
        """Continuous crawl loop — 60 s between rounds, auto-cleanup."""
        self._running = True
        round_num = 0
        try:
            while self._running:
                round_num += 1
                if self.status_callback:
                    self.status_callback(
                        f"═══ 第 {round_num} 轮采集开始 ═══")
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
        """Release all browser resources including cached sessions."""
        for crawler in self._crawlers.values():
            if hasattr(crawler, 'cleanup_session'):
                try:
                    await crawler.cleanup_session()
                except Exception:
                    pass
        self._crawlers.clear()
        try:
            await self.bm.close()
        except Exception:
            pass

    def stop(self):
        self._running = False
