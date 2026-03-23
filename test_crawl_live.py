#!/usr/bin/env python3
"""
Live crawl test against real Chinese social media sites.
Tests each crawler sequentially and reports collected data.
"""
import asyncio
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("test")

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
sys.path.insert(0, str(ROOT))


async def test_crawler(crawler_cls, bm):
    name = crawler_cls.platform_name
    crawler = crawler_cls(bm)
    logger.info("=" * 60)
    logger.info("TESTING: %s", name)
    logger.info("=" * 60)
    try:
        results = await crawler.fetch_tongcheng()
        logger.info("[%s] COLLECTED %d ITEMS", name, len(results))
        for i, r in enumerate(results[:8]):
            logger.info(
                "  #%d | %s | %s | %s",
                i + 1, r.nickname[:15], r.content[:50], r.link[:60],
            )
        if len(results) > 8:
            logger.info("  ... and %d more items", len(results) - 8)
        return name, len(results)
    except Exception as exc:
        logger.error("[%s] ERROR: %s", name, exc, exc_info=True)
        return name, -1


async def main():
    from src.crawlers.browser_manager import BrowserManager
    from src.crawlers.douyin import DouyinCrawler
    from src.crawlers.kuaishou import KuaishouCrawler
    from src.crawlers.xiaohongshu import XiaohongshuCrawler

    bm = BrowserManager(DATA_DIR)
    try:
        await bm.start()
        logger.info("Browser ready: %s", bm.is_ready)
    except Exception as exc:
        logger.error("Browser failed: %s", exc)
        return

    results = {}
    for cls in [DouyinCrawler, XiaohongshuCrawler, KuaishouCrawler]:
        name, count = await test_crawler(cls, bm)
        results[name] = count

    await bm.close()

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    total = 0
    for name, count in results.items():
        status = f"{count} items" if count >= 0 else "ERROR"
        print(f"  {name}: {status}")
        if count > 0:
            total += count
    print(f"  TOTAL: {total} items")
    print("=" * 60)
    sys.exit(0 if total > 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
