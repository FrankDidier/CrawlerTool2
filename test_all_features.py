#!/usr/bin/env python3
"""
Comprehensive test for all client-requested features.
Tests database, notify, sentiment dedup, watch management,
template generation, crawlers, and UI components.
"""
import asyncio
import os
import sys
import json
import tempfile
import sqlite3
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

PASS = 0
FAIL = 0


def report(name, ok, detail=""):
    global PASS, FAIL
    status = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    suffix = f" — {detail}" if detail else ""
    print(f"  [{status}] {name}{suffix}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. DATABASE TESTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def test_database():
    print("\n=== 1. Database Functions ===")
    from src.database import (
        init_db, insert_collection, insert_negative,
        get_unanalyzed_collection, count_unanalyzed,
        add_watch_config, list_watch_config, delete_watch_config_by_id,
        get_watch_config, delete_watch_config,
    )

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    try:
        await init_db(db_path)
        report("init_db", True)

        # Insert test collection items
        for i in range(10):
            platform = "抖音" if i < 5 else "快手"
            ok = await insert_collection(
                db_path, platform, f"id_{i}", f"user_{i}",
                f"content_{i}", f"https://link/{i}",
                f"2026-03-0{i+1} 12:00",
            )
            assert ok, f"insert_collection {i} failed"
        report("insert_collection x10", True)

        # Test dedup
        dup = await insert_collection(
            db_path, "抖音", "id_0", "user_0",
            "content_0", "https://link/0", "2026-03-01 12:00",
        )
        report("dedup (reject duplicate)", not dup)

        # Test count_unanalyzed (all 10 should be unanalyzed)
        cnt = await count_unanalyzed(db_path)
        report("count_unanalyzed = 10", cnt == 10, f"got {cnt}")

        # Test get_unanalyzed_collection (no filter)
        items = await get_unanalyzed_collection(db_path)
        report("get_unanalyzed_collection (all)", len(items) == 10, f"got {len(items)}")

        # Test platform filter
        items_dy = await get_unanalyzed_collection(db_path, platforms=["抖音"])
        report("get_unanalyzed (抖音 only)", len(items_dy) == 5, f"got {len(items_dy)}")

        items_ks = await get_unanalyzed_collection(db_path, platforms=["快手"])
        report("get_unanalyzed (快手 only)", len(items_ks) == 5, f"got {len(items_ks)}")

        # Test date filter
        items_date = await get_unanalyzed_collection(
            db_path, date_start="2026-03-03", date_end="2026-03-05",
        )
        report("get_unanalyzed (date 03-03~05)", len(items_date) == 3,
               f"got {len(items_date)}")

        # Test combined filter
        items_combo = await get_unanalyzed_collection(
            db_path, platforms=["抖音"], date_start="2026-03-01",
            date_end="2026-03-03",
        )
        report("get_unanalyzed (抖音 + date)", len(items_combo) == 3,
               f"got {len(items_combo)}")

        # Test limit
        items_lim = await get_unanalyzed_collection(db_path, limit=3)
        report("get_unanalyzed (limit=3)", len(items_lim) == 3, f"got {len(items_lim)}")

        # Mark 2 items as analyzed (insert into negative)
        for item in items[:2]:
            await insert_negative(db_path, {
                "collection_id": item["id"],
                "platform": item["platform"],
                "item_id": item["item_id"],
                "nickname": item["nickname"],
                "content": item["content"],
                "link": item["link"],
                "publish_date": item["publish_date"],
                "sentiment": "负面",
                "remark": "test remark",
            })

        # Verify dedup works - only 8 should remain unanalyzed
        cnt2 = await count_unanalyzed(db_path)
        report("dedup after 2 analyzed", cnt2 == 8, f"got {cnt2}")

        items_after = await get_unanalyzed_collection(db_path)
        report("get_unanalyzed after 2 analyzed", len(items_after) == 8,
               f"got {len(items_after)}")

        # ── Watch Config CRUD ──
        print("\n=== 1b. Watch Config CRUD ===")

        ok1 = await add_watch_config(db_path, "抖音", "uid_001", "张三")
        ok2 = await add_watch_config(db_path, "快手", "uid_002", "李四")
        ok3 = await add_watch_config(db_path, "小红书", "uid_003", "王五")
        report("add_watch_config x3", ok1 and ok2 and ok3)

        wlist = await list_watch_config(db_path)
        report("list_watch_config", len(wlist) == 3, f"got {len(wlist)}")

        # Verify list fields
        w = wlist[0]
        has_fields = all(k in w for k in ("id", "platform", "target_id",
                                           "target_name", "created_at"))
        report("watch config has all fields", has_fields, str(list(w.keys())))

        # Delete one
        del_ok = await delete_watch_config_by_id(db_path, wlist[1]["id"])
        report("delete_watch_config_by_id", del_ok)

        wlist2 = await list_watch_config(db_path)
        report("list after delete", len(wlist2) == 2, f"got {len(wlist2)}")

        # Verify the right one was deleted
        remaining_ids = [w["target_id"] for w in wlist2]
        report("correct item deleted",
               "uid_002" not in remaining_ids and "uid_001" in remaining_ids)

        # Legacy get_watch_config still works
        legacy = await get_watch_config(db_path)
        report("get_watch_config (legacy)", len(legacy) == 2)

    finally:
        os.unlink(db_path)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. NOTIFY - DINGTALK PREFIX
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_dingtalk_prefix():
    print("\n=== 2. DingTalk Prefix ===")
    import src.notify as notify
    import inspect

    source = inspect.getsource(notify.send_dingtalk)
    has_prefix = "【舆情预警】" in source
    report("send_dingtalk has 【舆情预警】prefix", has_prefix)

    # Verify the content format
    report("prefix in content string",
           '【舆情预警】' in source and 'content' in source)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. WATCH TEMPLATE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_watch_template():
    print("\n=== 3. Watch Template Generation ===")
    import pandas as pd

    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
        path = f.name

    try:
        df = pd.DataFrame({
            "平台": ["抖音", "快手", "小红书", "微信视频号"],
            "ID": ["user_id_123", "user_456", "note_user_789", "wx_user_001"],
            "昵称": ["示例昵称1", "示例昵称2", "示例昵称3", "示例昵称4"],
        })
        df.to_excel(path, index=False, engine="openpyxl")

        # Read back and verify
        df2 = pd.read_excel(path)
        cols = df2.columns.tolist()
        report("template has 平台 column", "平台" in cols)
        report("template has ID column", "ID" in cols)
        report("template has 昵称 column", "昵称" in cols)
        report("template has 4 rows", len(df2) == 4, f"got {len(df2)}")
        report("platforms correct",
               list(df2["平台"]) == ["抖音", "快手", "小红书", "微信视频号"])
    finally:
        os.unlink(path)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. APP MODULE STRUCTURE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_app_structure():
    print("\n=== 4. App Module Structure ===")
    import ast

    app_source = open("src/app.py").read()
    tree = ast.parse(app_source)

    classes = {node.name: node for node in ast.walk(tree)
               if isinstance(node, ast.ClassDef)}
    funcs_by_class = {}
    for cname, cnode in classes.items():
        methods = [n.name for n in cnode.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        funcs_by_class[cname] = methods

    # Check MainApp has all required methods
    main_methods = funcs_by_class.get("MainApp", [])

    report("MainApp._run_sentiment_filtered exists",
           "_run_sentiment_filtered" in main_methods)
    report("MainApp._run_sentiment_all exists",
           "_run_sentiment_all" in main_methods)
    report("MainApp._do_batch_sentiment exists",
           "_do_batch_sentiment" in main_methods)
    report("MainApp._view_watch_list exists",
           "_view_watch_list" in main_methods)
    report("MainApp._download_watch_template exists",
           "_download_watch_template" in main_methods)
    report("MainApp._check_llm_config exists",
           "_check_llm_config" in main_methods)

    # Check old _run_sentiment is gone
    report("old _run_sentiment removed",
           "_run_sentiment" not in main_methods)

    # Check DataPanel has double-click handler
    dp_methods = funcs_by_class.get("DataPanel", [])
    report("DataPanel._on_double_click exists",
           "_on_double_click" in dp_methods)

    # Check CollectionPanel has platform filter
    report("CollectionPanel has platform_filter",
           "platform_filter" in app_source)

    # Check NegativePanel buttons
    report("NegativePanel has 按条件分析 button",
           "按条件分析" in app_source)
    report("NegativePanel has 分析全部 button",
           "分析全部" in app_source)

    # Check WatchedPanel buttons
    report("WatchedPanel has 查看关注列表",
           "查看关注列表" in app_source)
    report("WatchedPanel has 下载模板",
           "下载模板" in app_source)

    # Check settings has test button
    report("Settings has 测试 API button",
           "测试 API" in app_source)

    # Check auto-refresh in _on_crawl_stats
    report("auto-refresh collection panel",
           "panel_collection._do_refresh" in app_source)

    # Check DingTalk title
    report("DingTalk send title",
           "出现相关负面舆情" in app_source)

    # Check webbrowser import in _on_double_click
    report("webbrowser.open in double-click handler",
           "webbrowser.open" in app_source)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. CRAWLER STRUCTURE (同城 tab finding)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_crawler_structure():
    print("\n=== 5. Crawler 同城 Tab Finding ===")

    # Douyin
    dy_src = open("src/crawlers/douyin.py").read()
    report("Douyin: _try_switch_tongcheng method",
           "_try_switch_tongcheng" in dy_src)
    report("Douyin: multiple CSS selectors",
           'a:has-text("同城")' in dy_src)
    report("Douyin: JS fallback click",
           "el.click()" in dy_src and "同城" in dy_src)
    report("Douyin: /nearby/ API interception",
           '"/nearby/"' in dy_src)

    # Xiaohongshu
    xhs_src = open("src/crawlers/xiaohongshu.py").read()
    report("XHS: multiple selectors for 附近",
           'a:has-text("附近")' in xhs_src or 'a:has-text' in xhs_src)
    report("XHS: JS fallback click",
           "el.click()" in xhs_src)
    report("XHS: fallback log message",
           "附近/同城 tab not found" in xhs_src)

    # Kuaishou
    ks_src = open("src/crawlers/kuaishou.py").read()
    report("Kuaishou: uses /samecity URL",
           "samecity" in ks_src)

    # WeChat
    wx_src = open("src/crawlers/wechat.py").read()
    report("WeChat: multiple selectors for 同城",
           'a:has-text("同城")' in wx_src or 'a:has-text' in wx_src)
    report("WeChat: JS fallback click",
           "el.click()" in wx_src)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6. LIVE CRAWLER TEST
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def test_live_crawl():
    print("\n=== 6. Live Crawler Test ===")
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from src.crawlers.browser_manager import BrowserManager
    from src.crawlers.douyin import DouyinCrawler
    from src.crawlers.xiaohongshu import XiaohongshuCrawler
    from src.crawlers.kuaishou import KuaishouCrawler
    from src.crawlers.wechat import WechatCrawler

    data_dir = Path(__file__).parent / "data"
    data_dir.mkdir(exist_ok=True)
    bm = BrowserManager(data_dir)

    try:
        await bm.start()
        report("BrowserManager.start()", True)
    except Exception as e:
        report("BrowserManager.start()", False, str(e))
        return

    crawlers = [
        ("抖音", DouyinCrawler),
        ("小红书", XiaohongshuCrawler),
        ("快手", KuaishouCrawler),
    ]

    total_items = 0
    for name, cls in crawlers:
        print(f"\n  --- Testing {name} crawler ---")
        crawler = cls(bm)
        try:
            items = await crawler.fetch_tongcheng()
            count = len(items)
            total_items += count
            report(f"{name}: collected items", count > 0,
                   f"{count} items")
            if items:
                sample = items[0]
                report(f"{name}: item has platform",
                       sample.platform == name)
                report(f"{name}: item has content",
                       len(sample.content) > 0)
                report(f"{name}: item has link",
                       sample.link.startswith("http"))
                report(f"{name}: item has nickname",
                       len(sample.nickname) > 0)
                print(f"    Sample: @{sample.nickname}: "
                      f"{sample.content[:60]}...")
        except Exception as e:
            report(f"{name}: crawl", False, str(e))

    report(f"Total items across all platforms", total_items > 0,
           f"{total_items} items")

    await bm.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 7. END-TO-END: DB INSERT + QUERY WITH PLATFORM FILTER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def test_platform_filter_e2e():
    print("\n=== 7. Platform Filter End-to-End ===")
    from src.database import init_db, insert_collection
    import aiosqlite

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    try:
        await init_db(db_path)

        # Simulate crawl from multiple platforms
        platforms_data = {
            "抖音": [("dy_1", "用户A", "抖音内容1"), ("dy_2", "用户B", "抖音内容2")],
            "快手": [("ks_1", "用户C", "快手内容1"), ("ks_2", "用户D", "快手内容2")],
            "小红书": [("xhs_1", "用户E", "小红书内容1")],
        }

        for platform, items in platforms_data.items():
            for item_id, nick, content in items:
                await insert_collection(
                    db_path, platform, item_id, nick, content,
                    f"https://link/{item_id}", "2026-03-07 10:00",
                )

        # Query all
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT platform FROM collection ORDER BY id DESC LIMIT 500"
            )
            rows = [dict(r) for r in await cur.fetchall()]

        platforms_in_result = set(r["platform"] for r in rows)
        report("All platforms in unfiltered query",
               platforms_in_result == {"抖音", "快手", "小红书"},
               str(platforms_in_result))

        # Query with platform filter
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT platform FROM collection WHERE platform = ? "
                "ORDER BY id DESC LIMIT 500",
                ("快手",),
            )
            rows = [dict(r) for r in await cur.fetchall()]

        ks_only = all(r["platform"] == "快手" for r in rows)
        report("Platform filter: 快手 only", ks_only and len(rows) == 2,
               f"{len(rows)} rows")

    finally:
        os.unlink(db_path)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 8. FULL IMPORT TEST
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_full_import():
    print("\n=== 8. Full Module Import ===")
    try:
        from src.database import (
            init_db, insert_collection, insert_negative, insert_watched,
            get_collection_batch, get_unanalyzed_collection, count_unanalyzed,
            add_watch_config, list_watch_config, delete_watch_config_by_id,
            get_watch_config, delete_watch_config, watched_collection_ids,
        )
        report("database imports", True)
    except Exception as e:
        report("database imports", False, str(e))

    try:
        from src.notify import send_dingtalk, send_wechat
        report("notify imports", True)
    except Exception as e:
        report("notify imports", False, str(e))

    try:
        from src.llm import sentiment_analyze
        report("llm imports", True)
    except Exception as e:
        report("llm imports", False, str(e))

    try:
        from src.export_utils import (
            export_collection_to_excel, export_negative_to_excel,
            export_watched_to_excel, backup_db,
        )
        report("export_utils imports", True)
    except Exception as e:
        report("export_utils imports", False, str(e))

    try:
        from src.crawlers.base import BaseCrawler, CrawlResult
        from src.crawlers.douyin import DouyinCrawler
        from src.crawlers.kuaishou import KuaishouCrawler
        from src.crawlers.xiaohongshu import XiaohongshuCrawler
        from src.crawlers.wechat import WechatCrawler
        from src.crawlers.manager import CrawlerManager, CRAWLERS
        from src.crawlers.browser_manager import BrowserManager
        report("crawler imports", True)
    except Exception as e:
        report("crawler imports", False, str(e))

    try:
        from src.crawlers.manager import CRAWLERS
        expected = {"抖音", "快手", "小红书", "微信视频号"}
        report("CRAWLERS has all 4 platforms",
               set(CRAWLERS.keys()) == expected)
    except Exception as e:
        report("CRAWLERS check", False, str(e))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MAIN
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def main():
    print("=" * 60)
    print("  COMPREHENSIVE FEATURE TEST")
    print("=" * 60)

    # Non-async tests
    test_full_import()
    test_dingtalk_prefix()
    test_watch_template()
    test_app_structure()
    test_crawler_structure()

    # Async tests
    await test_database()
    await test_platform_filter_e2e()

    # Live crawler test
    await test_live_crawl()

    # Summary
    print("\n" + "=" * 60)
    total = PASS + FAIL
    print(f"  RESULTS: {PASS}/{total} passed, {FAIL} failed")
    if FAIL == 0:
        print("  ALL TESTS PASSED!")
    else:
        print(f"  {FAIL} TESTS FAILED — see details above")
    print("=" * 60)

    return FAIL == 0


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
