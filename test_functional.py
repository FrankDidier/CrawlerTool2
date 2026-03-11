#!/usr/bin/env python3
"""
功能测试 - 验证业务流程与预期一致
模拟完整用户操作流程，无 GUI
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
TEST_DB = DATA_DIR / "func_test.db"


def run(coro):
    return asyncio.run(coro)


async def test_full_workflow():
    """完整业务流程测试"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if TEST_DB.exists():
        TEST_DB.unlink()
    db = TEST_DB

    from src import database, auth
    from src.crawlers.manager import CrawlerManager, CRAWLERS
    from src.export_utils import (
        export_collection_to_excel,
        export_negative_to_excel,
        export_watched_to_excel,
        backup_db,
    )
    from src.notify import send_dingtalk, send_wechat

    errs = []

    def ok(name: str):
        print(f"  ✓ {name}")

    def fail(name: str, msg: str):
        errs.append(f"{name}: {msg}")
        print(f"  ✗ {name}: {msg}")

    print("\n" + "=" * 60)
    print("功能测试 - 完整业务流程")
    print("=" * 60)

    # --- 1. 初始化与登录 ---
    print("\n[1] 初始化与认证")
    await database.init_db(db)
    await auth.init_auth_db(db)
    user = await auth.login(db, "admin", "admin123")
    if not user or user["role"] != "admin":
        fail("管理员登录", "应能使用 admin/admin123 登录")
    else:
        ok("管理员登录")

    wrong = await auth.login(db, "admin", "wrong")
    if wrong is not None:
        fail("错误密码", "错误密码应拒绝")
    else:
        ok("错误密码拒绝")

    ok_add, _ = await auth.add_user(db, "worker1", "pass123", "user")
    if not ok_add:
        fail("添加用户", "应能添加普通用户")
    else:
        ok("添加普通用户")

    worker = await auth.login(db, "worker1", "pass123")
    if not worker or worker["role"] != "user":
        fail("普通用户登录", "应能登录")
    else:
        ok("普通用户登录")

    # --- 2. 数据采集与去重 ---
    print("\n[2] 数据采集与去重")
    r1 = await database.insert_collection(
        db, "抖音", "user_001", "测试用户A", "这是一条中性内容", "https://dy.com/1", "2024-03-01"
    )
    r2 = await database.insert_collection(
        db, "抖音", "user_002", "测试用户B", "这是负面内容，非常不满", "https://dy.com/2", "2024-03-02"
    )
    r3 = await database.insert_collection(
        db, "抖音", "user_001", "测试用户A", "这是一条中性内容", "https://dy.com/1", "2024-03-01"
    )
    if not r1 or not r2:
        fail("采集插入", "新数据应插入成功")
    else:
        ok("新数据插入")
    if r3:
        fail("去重", "完全重复数据应被跳过")
    else:
        ok("去重机制")

    batch = await database.get_collection_batch(db, limit=10)
    if len(batch) < 2:
        fail("分批获取", f"应至少 2 条，实际 {len(batch)}")
    else:
        ok(f"分批获取 {len(batch)} 条")

    # --- 3. 关注对象与匹配 ---
    print("\n[3] 关注对象与匹配")
    await database.add_watch_config(db, "抖音", "user_001", "测试用户A")
    watch_list = await database.get_watch_config(db)
    if len(watch_list) < 1:
        fail("添加关注", "关注列表应有数据")
    else:
        ok("添加关注对象")

    already = await database.watched_collection_ids(db)
    for r in batch:
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
                break

    async with __import__("aiosqlite").connect(db) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM watched")
        watched_count = (await cur.fetchone())[0]
    if watched_count < 1:
        fail("关注匹配", f"应有匹配记录，实际 {watched_count}")
    else:
        ok(f"关注匹配 {watched_count} 条")

    # --- 4. 语义判断（模拟大模型返回）---
    print("\n[4] 语义判断")
    batch_for_llm = await database.get_collection_batch(db, limit=20)
    mock_results = [
        {"sentiment": "中性", "remark": "正常"},
        {"sentiment": "负面", "remark": "表达不满"},
    ]
    while len(mock_results) < len(batch_for_llm):
        mock_results.append({"sentiment": "中性", "remark": ""})
    results = mock_results[: len(batch_for_llm)]

    for i, r in enumerate(batch_for_llm):
        if i < len(results) and results[i].get("sentiment") == "负面":
            await database.insert_negative(db, {
                **r,
                "collection_id": r["id"],
                "sentiment": "负面",
                "remark": results[i].get("remark", ""),
            })

    async with __import__("aiosqlite").connect(db) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM negative")
        neg_count = (await cur.fetchone())[0]
    if neg_count < 1:
        fail("负面入库", f"应有负面记录，实际 {neg_count}")
    else:
        ok(f"负面言论入库 {neg_count} 条")

    # --- 5. 导出 ---
    print("\n[5] Excel 导出")
    out_dir = DATA_DIR / "func_test_export"
    out_dir.mkdir(exist_ok=True)
    export_collection_to_excel(db, out_dir / "collection.xlsx")
    export_negative_to_excel(db, out_dir / "negative.xlsx")
    export_watched_to_excel(db, out_dir / "watched.xlsx")
    for name in ["collection.xlsx", "negative.xlsx", "watched.xlsx"]:
        p = out_dir / name
        if not p.exists() or p.stat().st_size < 100:
            fail(f"导出 {name}", "文件应存在且非空")
        else:
            ok(f"导出 {name}")

    # --- 6. 备份 ---
    print("\n[6] 数据备份")
    backup_path = backup_db(db, out_dir / "backups")
    if not Path(backup_path).exists():
        fail("备份", "备份文件应存在")
    else:
        ok("数据库备份")

    # --- 7. 爬虫管理器 ---
    print("\n[7] 爬虫管理器")
    mgr = CrawlerManager(db, list(CRAWLERS.keys()))
    stats = await mgr.run_once()
    if not isinstance(stats, dict) or len(stats) != 4:
        fail("爬虫运行", f"应返回 4 平台统计")
    else:
        ok(f"爬虫单次运行 {list(stats.keys())}")

    # --- 8. 权限 ---
    print("\n[8] 权限检查")
    if not auth.can_modify_db("admin"):
        fail("管理员权限", "admin 应能修改 DB")
    else:
        ok("管理员可修改 DB")
    if auth.can_modify_db("user"):
        fail("普通用户权限", "user 不应能修改 DB")
    else:
        ok("普通用户不可修改 DB")

    # --- 9. 大模型无 API 时返回默认 ---
    print("\n[9] 大模型接口（未配置时）")
    from src.llm import sentiment_analyze
    empty_results = await sentiment_analyze("", "", "", ["测试"])
    if not empty_results or empty_results[0].get("sentiment") != "neutral":
        fail("LLM 未配置", f"应返回默认值，实际 {empty_results}")
    else:
        ok("LLM 未配置时默认中性")

    # --- 10. 通知（空 URL 不报错）---
    print("\n[10] 通知接口")
    d = send_dingtalk("", "t", "x")
    w = send_wechat("", "t", "x")
    if d is not False or w is not False:
        fail("空 URL", "空 webhook 应返回 False 不抛错")
    else:
        ok("通知接口容错")

    # --- 11. Excel 导入关注 ---
    print("\n[11] Excel 导入关注")
    import pandas as pd
    excel_path = out_dir / "watch_import_test.xlsx"
    pd.DataFrame([["快手", "ks_001", "快手用户"], ["小红书", "xhs_002", ""]]).to_excel(
        excel_path, index=False, header=["平台", "ID", "昵称"]
    )
    for _, row in pd.read_excel(excel_path).iterrows():
        platform = str(row.get("平台", "")).strip()
        tid = str(row.get("ID", "")).strip()
        tname = str(row.get("昵称", "")).strip() if pd.notna(row.get("昵称")) else ""
        if platform and tid and platform in CRAWLERS:
            await database.add_watch_config(db, platform, tid, tname or "")

    w2 = await database.get_watch_config(db)
    if len(w2) < 3:
        fail("Excel 导入", f"应至少 3 个关注对象，实际 {len(w2)}")
    else:
        ok(f"Excel 导入关注 {len(w2)} 个")

    # --- 结果 ---
    print("\n" + "=" * 60)
    if errs:
        print("失败项:")
        for e in errs:
            print("  -", e)
        print("=" * 60)
        return 1
    print("全部功能测试通过 ✓")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(run(test_full_workflow()))
