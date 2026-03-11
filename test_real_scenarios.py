#!/usr/bin/env python3
"""
真实场景端到端测试

不只是调用函数检查返回值——
而是模拟完整业务流，注入真实数据，查 DB 行数/内容，
读回 Excel 验证内容，跑完整采集管线，等等。
"""
import asyncio
import sqlite3
import sys
import os
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
TEST_DB = DATA_DIR / "scenario_test.db"
EXPORT_DIR = DATA_DIR / "scenario_export"

passed = 0
failed = 0
fail_details = []


def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        msg = f"{name}: {detail}" if detail else name
        fail_details.append(msg)
        print(f"  ✗ {name}" + (f" — {detail}" if detail else ""))


def query_db(sql: str, params=()):
    """直接用 sqlite3 读测试库，不走 async，验证数据确实落盘"""
    con = sqlite3.connect(TEST_DB)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(sql, params).fetchall()]
    con.close()
    return rows


def count_db(table: str) -> int:
    con = sqlite3.connect(TEST_DB)
    n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    con.close()
    return n


async def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    if TEST_DB.exists():
        TEST_DB.unlink()

    from src import database, auth
    from src.crawlers.base import CrawlResult
    from src.crawlers.manager import CrawlerManager, CRAWLERS
    from src.export_utils import (
        export_collection_to_excel,
        export_negative_to_excel,
        export_watched_to_excel,
        backup_db,
    )
    from src.llm import sentiment_analyze
    import pandas as pd

    db = TEST_DB

    print("\n" + "=" * 60)
    print("  真实场景端到端测试")
    print("=" * 60)

    # ═══════════════════════════════════════════════════
    # SCENARIO 1: 完整采集管线（注入 → DB → 去重 → 验证）
    # ═══════════════════════════════════════════════════
    print("\n─── 场景 1: 完整采集管线 ───")

    await database.init_db(db)
    await auth.init_auth_db(db)

    FAKE_DATA = {
        "抖音": [
            CrawlResult("抖音", "dy_001", "张三", "今天天气真好，同城打卡", "https://douyin.com/v/001", "2024-06-15 10:30"),
            CrawlResult("抖音", "dy_002", "李四", "这个城市的管理太差了，到处都是垃圾", "https://douyin.com/v/002", "2024-06-15 11:00"),
            CrawlResult("抖音", "dy_003", "王五", "周末一起去公园吧", "https://douyin.com/v/003", "2024-06-15 12:00"),
        ],
        "快手": [
            CrawlResult("快手", "ks_001", "赵六", "同城美食推荐，这家店太好吃了", "https://kuaishou.com/v/001", "2024-06-15 09:00"),
            CrawlResult("快手", "ks_002", "张三", "政府不作为，投诉无门", "https://kuaishou.com/v/002", "2024-06-15 13:00"),
        ],
        "小红书": [
            CrawlResult("小红书", "xhs_001", "小美", "同城探店｜这家咖啡绝了", "https://xiaohongshu.com/p/001", "2024-06-15 08:30"),
            CrawlResult("小红书", "xhs_002", "小明", "投诉！小区物业乱收费", "https://xiaohongshu.com/p/002", "2024-06-15 14:00"),
        ],
        "微信视频号": [
            CrawlResult("微信视频号", "wx_001", "老王", "同城钓鱼好去处", "https://channels.weixin.qq.com/001", "2024-06-15 07:00"),
        ],
    }

    # Monkey-patch crawlers to return fake data
    original_methods = {}
    for platform_name, cls in CRAWLERS.items():
        original_methods[platform_name] = cls.fetch_tongcheng
        items = FAKE_DATA.get(platform_name, [])

        async def make_fetch(self, _items=items):
            return _items
        cls.fetch_tongcheng = make_fetch

    # Run crawl pipeline through the real CrawlerManager
    mgr = CrawlerManager(db, list(CRAWLERS.keys()))
    stats = await mgr.run_once()

    total_new = sum(n for n, _ in stats.values())
    check("采集入库总数", total_new == 8, f"期望 8，实际 {total_new}")

    # Verify data actually in DB via raw sqlite3
    rows = query_db("SELECT * FROM collection ORDER BY id")
    check("DB 实际行数", len(rows) == 8, f"期望 8，实际 {len(rows)}")

    # Verify fields populated correctly
    if rows:
        # asyncio.gather 并行执行，入库顺序不固定，按 item_id 找抖音的 dy_001
        dy001 = [r for r in rows if r["item_id"] == "dy_001"]
        check("字段: 找到 dy_001", len(dy001) == 1)
        if dy001:
            row0 = dy001[0]
            check("字段: platform", row0["platform"] == "抖音")
            check("字段: item_id", row0["item_id"] == "dy_001")
            check("字段: nickname", row0["nickname"] == "张三")
        # 对所有行做通用验证
        check("字段: content 全部非空", all(len(r["content"]) > 0 for r in rows))
        check("字段: link 全部合法", all(r["link"].startswith("http") for r in rows))
        check("字段: publish_date 全部非空", all(len(r["publish_date"]) > 0 for r in rows))
        check("字段: dedup_key 全部非空", all(len(r["dedup_key"]) > 0 for r in rows))
        check("字段: created_at 全部存在", all(r["created_at"] is not None for r in rows))

        platforms_in_db = set(r["platform"] for r in rows)
        check("4 平台均入库", platforms_in_db == {"抖音", "快手", "小红书", "微信视频号"}, f"实际: {platforms_in_db}")
    else:
        check("字段验证", False, "无数据可验证")

    # ═══════════════════════════════════════════════════
    # SCENARIO 2: 去重 — 重跑完全相同数据
    # ═══════════════════════════════════════════════════
    print("\n─── 场景 2: 去重机制 ───")

    stats2 = await mgr.run_once()
    total_new2 = sum(n for n, _ in stats2.values())
    total_skip2 = sum(s for _, s in stats2.values())
    check("重复数据：新增 0", total_new2 == 0, f"期望 0，实际 {total_new2}")
    check("重复数据：跳过 8", total_skip2 == 8, f"期望 8，实际 {total_skip2}")
    check("DB 行数不变", count_db("collection") == 8)

    # Same user, different content → should insert
    await database.insert_collection(db, "抖音", "dy_001", "张三", "这是一条全新的内容", "https://douyin.com/v/new", "2024-06-16")
    check("同用户不同内容：新增", count_db("collection") == 9)

    # ═══════════════════════════════════════════════════
    # SCENARIO 3: 关注对象匹配（精确 + 跨平台）
    # ═══════════════════════════════════════════════════
    print("\n─── 场景 3: 关注对象匹配 ───")

    # 关注 "张三" in 抖音, "小明" in 小红书
    await database.add_watch_config(db, "抖音", "dy_001", "张三")
    await database.add_watch_config(db, "小红书", "xhs_002", "小明")

    # Run the matching logic (same as app._check_watchlist)
    watch_list = await database.get_watch_config(db)
    already = await database.watched_collection_ids(db)
    all_rows = await database.get_collection_batch(db, limit=500)
    matched_names = []
    for r in all_rows:
        if r["id"] in already:
            continue
        for w in watch_list:
            if w["platform"] == r["platform"] and (
                w["target_id"] == r.get("item_id") or w["target_name"] == r.get("nickname")
            ):
                await database.insert_watched(db, {
                    **r,
                    "collection_id": r["id"],
                    "watch_target_id": w["target_id"],
                    "watch_target_name": w.get("target_name", ""),
                })
                matched_names.append((w["target_name"], r["platform"]))
                break

    watched_rows = query_db("SELECT * FROM watched")
    check("关注匹配数", len(watched_rows) >= 2, f"期望 ≥2 (张三抖音×2 + 小明小红书)，实际 {len(watched_rows)}")

    # 验证匹配了正确的人
    watched_targets = set((r["watch_target_name"], r["platform"]) for r in watched_rows)
    check("匹配了张三(抖音)", ("张三", "抖音") in watched_targets, f"实际: {watched_targets}")
    check("匹配了小明(小红书)", ("小明", "小红书") in watched_targets, f"实际: {watched_targets}")

    # 张三在快手也有数据，但关注配置只关注他的抖音，不应匹配
    ks_watched = [r for r in watched_rows if r["platform"] == "快手"]
    check("快手张三不匹配(仅关注抖音)", len(ks_watched) == 0, f"快手匹配数: {len(ks_watched)}")

    # 重跑匹配：不应重复入库
    already2 = await database.watched_collection_ids(db)
    count_before = count_db("watched")
    all_rows2 = await database.get_collection_batch(db, limit=500)
    for r in all_rows2:
        if r["id"] in already2:
            continue
        for w in watch_list:
            if w["platform"] == r["platform"] and (
                w["target_id"] == r.get("item_id") or w["target_name"] == r.get("nickname")
            ):
                await database.insert_watched(db, {
                    **r, "collection_id": r["id"],
                    "watch_target_id": w["target_id"],
                    "watch_target_name": w.get("target_name", ""),
                })
                break
    check("关注匹配不重复入库", count_db("watched") == count_before)

    # ═══════════════════════════════════════════════════
    # SCENARIO 4: 语义判断 → 负面入库 全链路
    # ═══════════════════════════════════════════════════
    print("\n─── 场景 4: 语义判断 + 负面入库 ───")

    batch = await database.get_collection_batch(db, limit=50)
    texts = [r.get("content", "") for r in batch]

    # 无 API key → 应返回安全默认值
    results_no_key = await sentiment_analyze("", "", "", texts)
    check("无 API: 返回数量匹配", len(results_no_key) == len(texts))
    check("无 API: 不崩溃", all("sentiment" in r for r in results_no_key))

    # 模拟真实大模型返回
    fake_sentiments = []
    for t in texts:
        if any(kw in t for kw in ["差", "垃圾", "不作为", "投诉", "乱收费"]):
            fake_sentiments.append({"sentiment": "负面", "remark": "包含负面关键词"})
        elif any(kw in t for kw in ["好", "推荐", "绝了", "美食"]):
            fake_sentiments.append({"sentiment": "正面", "remark": "正面评价"})
        else:
            fake_sentiments.append({"sentiment": "中性", "remark": ""})

    neg_before = count_db("negative")
    for i, r in enumerate(batch):
        if i < len(fake_sentiments) and fake_sentiments[i]["sentiment"] == "负面":
            await database.insert_negative(db, {
                **r,
                "collection_id": r["id"],
                "sentiment": "负面",
                "remark": fake_sentiments[i]["remark"],
            })
    neg_after = count_db("negative")
    neg_added = neg_after - neg_before

    # 数据中有 3 条负面："管理太差" "政府不作为" "物业乱收费"
    check("负面入库数量", neg_added == 3, f"期望 3，实际 {neg_added}")

    # 验证负面内容确实是负面的
    neg_rows = query_db("SELECT * FROM negative")
    neg_contents = [r["content"] for r in neg_rows]
    check("负面内容含「垃圾」", any("垃圾" in c for c in neg_contents))
    check("负面内容含「不作为」", any("不作为" in c for c in neg_contents))
    check("负面内容含「乱收费」", any("乱收费" in c for c in neg_contents))
    # 正面/中性不应在负面库
    check("正面不在负面库", not any("好吃" in c for c in neg_contents))
    check("中性不在负面库", not any("公园" in c for c in neg_contents))

    # 每条负面都有 remark
    check("所有负面有备注", all(r["remark"] for r in neg_rows))

    # ═══════════════════════════════════════════════════
    # SCENARIO 5: Excel 导出 → 读回验证内容
    # ═══════════════════════════════════════════════════
    print("\n─── 场景 5: Excel 导出 + 读回验证 ───")

    p1 = EXPORT_DIR / "collection.xlsx"
    p2 = EXPORT_DIR / "negative.xlsx"
    p3 = EXPORT_DIR / "watched.xlsx"

    export_collection_to_excel(db, p1)
    export_negative_to_excel(db, p2)
    export_watched_to_excel(db, p3)

    # Read back and verify
    df1 = pd.read_excel(p1)
    check("Excel 采集: 行数", len(df1) == count_db("collection"), f"Excel {len(df1)} vs DB {count_db('collection')}")
    check("Excel 采集: 列名", set(df1.columns) >= {"platform", "item_id", "nickname", "content"})
    check("Excel 采集: 内容不为空", df1["content"].notna().all())
    check("Excel 采集: 4 平台", set(df1["platform"].unique()) == {"抖音", "快手", "小红书", "微信视频号"})

    df2 = pd.read_excel(p2)
    check("Excel 负面: 行数", len(df2) == count_db("negative"), f"Excel {len(df2)} vs DB {count_db('negative')}")
    check("Excel 负面: 全部为负面", (df2["sentiment"] == "负面").all())
    check("Excel 负面: 有备注列", "remark" in df2.columns)

    df3 = pd.read_excel(p3)
    check("Excel 关注: 行数", len(df3) == count_db("watched"), f"Excel {len(df3)} vs DB {count_db('watched')}")

    # ═══════════════════════════════════════════════════
    # SCENARIO 6: 备份 → 恢复验证
    # ═══════════════════════════════════════════════════
    print("\n─── 场景 6: 备份与恢复 ───")

    backup_path = backup_db(db, EXPORT_DIR / "backups")
    check("备份文件存在", Path(backup_path).exists())
    check("备份文件大小 > 0", Path(backup_path).stat().st_size > 0)

    # 读备份库，验证数据完整
    bcon = sqlite3.connect(backup_path)
    bcount = bcon.execute("SELECT COUNT(*) FROM collection").fetchone()[0]
    bcon.close()
    check("备份数据完整", bcount == count_db("collection"), f"备份 {bcount} vs 原库 {count_db('collection')}")

    # ═══════════════════════════════════════════════════
    # SCENARIO 7: 认证边界用例
    # ═══════════════════════════════════════════════════
    print("\n─── 场景 7: 认证边界 ───")

    check("空用户名拒绝", await auth.login(db, "", "admin123") is None)
    check("空密码拒绝", await auth.login(db, "admin", "") is None)
    check("SQL 注入安全", await auth.login(db, "admin' OR '1'='1", "x") is None)
    check("超长用户名", await auth.login(db, "a" * 10000, "b") is None)

    ok_dup, msg_dup = await auth.add_user(db, "admin", "xxx", "admin")
    check("重复用户名拒绝", not ok_dup, msg_dup)

    ok_bad_role, msg_bad_role = await auth.add_user(db, "hacker", "pass", "superadmin")
    check("无效角色拒绝", not ok_bad_role)

    # 密码修改
    await auth.change_password(db, "admin", "newpass999")
    check("旧密码失效", await auth.login(db, "admin", "admin123") is None)
    check("新密码生效", (await auth.login(db, "admin", "newpass999")) is not None)
    await auth.change_password(db, "admin", "admin123")  # restore

    # ═══════════════════════════════════════════════════
    # SCENARIO 8: 单平台故障不影响其他
    # ═══════════════════════════════════════════════════
    print("\n─── 场景 8: 单平台故障隔离 ───")

    # 让抖音爬虫抛异常
    async def broken_fetch(self):
        raise RuntimeError("模拟抖音接口崩溃")

    CRAWLERS["抖音"].fetch_tongcheng = broken_fetch

    async def ks_new_data(self):
        return [CrawlResult("快手", "ks_NEW", "新用户", "新内容", "https://ks.com/new", "2024-07-01")]
    CRAWLERS["快手"].fetch_tongcheng = ks_new_data

    before = count_db("collection")
    stats3 = await mgr.run_once()
    after = count_db("collection")

    check("抖音崩溃不影响运行", True)  # 如果到这里没崩就行
    check("快手新数据入库", after > before, f"前 {before} 后 {after}")
    check("抖音统计为 (0,0)", stats3.get("抖音") == (0, 0), f"实际: {stats3.get('抖音')}")

    # Restore crawlers
    for platform_name, cls in CRAWLERS.items():
        cls.fetch_tongcheng = original_methods[platform_name]

    # ═══════════════════════════════════════════════════
    # SCENARIO 9: Excel 导入关注对象（含边界数据）
    # ═══════════════════════════════════════════════════
    print("\n─── 场景 9: Excel 导入关注 ───")

    excel_path = EXPORT_DIR / "watch_import.xlsx"
    test_df = pd.DataFrame([
        {"平台": "抖音", "ID": "import_001", "昵称": "导入用户A"},
        {"平台": "快手", "ID": "import_002", "昵称": ""},       # 昵称为空
        {"平台": "无效平台", "ID": "bad_001", "昵称": "坏人"},   # 无效平台
        {"平台": "小红书", "ID": "", "昵称": "无ID"},             # ID 为空
        {"平台": "微信视频号", "ID": "import_003", "昵称": "导入用户C"},
    ])
    test_df.to_excel(excel_path, index=False)

    # Simulate the import logic from app (including NaN handling)
    df = pd.read_excel(excel_path)
    imported = 0
    for _, row in df.iterrows():
        platform = str(row.get("平台", "")).strip()
        tid = str(row.get("ID", "")).strip()
        tname = str(row.get("昵称", "")).strip() if pd.notna(row.get("昵称")) else ""
        if tid.lower() == "nan":
            tid = ""
        if tname.lower() == "nan":
            tname = ""
        if platform and tid and platform in CRAWLERS:
            ok = await database.add_watch_config(db, platform, tid, tname)
            if ok:
                imported += 1

    check("有效行导入", imported == 3, f"期望 3 (抖音+快手+视频号)，实际 {imported}")
    # "无效平台" 和空 ID 应被跳过
    all_watch = await database.get_watch_config(db)
    bad_platform = [w for w in all_watch if w["platform"] == "无效平台"]
    check("无效平台不入库", len(bad_platform) == 0)

    # ═══════════════════════════════════════════════════
    # SCENARIO 10: 数据库完整性最终校验
    # ═══════════════════════════════════════════════════
    print("\n─── 场景 10: 数据完整性 ───")

    final_collection = count_db("collection")
    final_negative = count_db("negative")
    final_watched = count_db("watched")
    final_watch_config = count_db("watch_config")
    final_users = count_db("users")

    check("采集库有数据", final_collection > 0, f"{final_collection} 条")
    check("负面库有数据", final_negative > 0, f"{final_negative} 条")
    check("关注对象库有数据", final_watched > 0, f"{final_watched} 条")
    check("关注配置有数据", final_watch_config > 0, f"{final_watch_config} 条")
    check("用户表有数据", final_users >= 1, f"{final_users} 个用户")

    # Foreign key: negative.collection_id 应该指向真实的 collection 记录
    orphan_neg = query_db(
        "SELECT n.id FROM negative n LEFT JOIN collection c ON n.collection_id = c.id WHERE c.id IS NULL"
    )
    check("负面表外键完整", len(orphan_neg) == 0, f"{len(orphan_neg)} 条孤立记录")

    # Watched foreign key
    orphan_watch = query_db(
        "SELECT w.id FROM watched w LEFT JOIN collection c ON w.collection_id = c.id WHERE c.id IS NULL"
    )
    check("关注表外键完整", len(orphan_watch) == 0, f"{len(orphan_watch)} 条孤立记录")

    # ════════════════════ SUMMARY ════════════════════
    print(f"\n{'=' * 60}")
    print(f"  结果: {passed}/{passed + failed} 通过, {failed} 失败")
    if fail_details:
        print("\n  失败项:")
        for d in fail_details:
            print(f"    ✗ {d}")
    else:
        print("  全部场景测试通过 ✓")
    print("=" * 60 + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
