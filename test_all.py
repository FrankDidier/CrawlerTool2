#!/usr/bin/env python3
"""
综合测试脚本 - 验证各模块功能
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "crawler.db"

# 使用测试数据库避免污染正式数据
TEST_DB = ROOT / "data" / "test.db"


async def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # 使用全新测试库，避免上次运行残留
    if TEST_DB.exists():
        TEST_DB.unlink()
    db = TEST_DB

    print("=" * 50)
    print("爬虫小工具 - 综合测试")
    print("=" * 50)

    # 1. 数据库初始化
    print("\n[1] 数据库初始化...")
    from src import database, auth
    await database.init_db(db)
    await auth.init_auth_db(db)
    print("  OK")

    # 2. 登录测试
    print("\n[2] 登录测试...")
    user = await auth.login(db, "admin", "admin123")
    assert user is not None, "admin 登录失败"
    assert user["role"] == "admin", "角色错误"
    print(f"  OK - 登录用户: {user['username']} ({user['role']})")

    # 3. 添加用户测试
    print("\n[3] 添加用户测试...")
    ok, msg = await auth.add_user(db, "testuser", "test123", "user")
    assert ok, f"添加用户失败: {msg}"
    print(f"  OK - {msg}")

    # 4. 采集数据插入与去重
    print("\n[4] 采集数据插入与去重...")
    r1 = await database.insert_collection(db, "抖音", "id1", "用户A", "内容1", "http://a.com", "2024-01-01")
    r2 = await database.insert_collection(db, "抖音", "id1", "用户A", "内容1", "http://a.com", "2024-01-01")
    assert r1 is True, "首次插入应成功"
    assert r2 is False, "重复插入应被跳过"
    print("  OK - 去重机制正常")

    # 5. 分批获取
    print("\n[5] 分批获取采集数据...")
    batch = await database.get_collection_batch(db, limit=10)
    assert len(batch) >= 1, "应能获取到数据"
    print(f"  OK - 获取 {len(batch)} 条")

    # 6. 关注对象配置
    print("\n[6] 关注对象配置...")
    await database.add_watch_config(db, "抖音", "id1", "用户A")
    watch_list = await database.get_watch_config(db)
    assert len(watch_list) >= 1, "关注列表应有数据"
    print(f"  OK - 关注对象数: {len(watch_list)}")

    # 7. 负面言论插入
    print("\n[7] 负面言论入库...")
    row = batch[0]
    await database.insert_negative(db, {
        **row, "collection_id": row["id"], "sentiment": "负面", "remark": "测试备注",
    })
    print("  OK")

    # 8. 爬虫管理器单次运行
    print("\n[8] 爬虫管理器（单次运行）...")
    from src.crawlers.manager import CrawlerManager, CRAWLERS
    mgr = CrawlerManager(db, list(CRAWLERS.keys()))
    stats = await mgr.run_once()
    print(f"  OK - 各平台统计: {stats}")

    # 9. 导出
    print("\n[9] Excel 导出...")
    from src.export_utils import export_collection_to_excel, export_negative_to_excel
    out1 = ROOT / "data" / "test_export_collection.xlsx"
    export_collection_to_excel(db, out1)
    assert out1.exists(), "导出文件应存在"
    out2 = ROOT / "data" / "test_export_negative.xlsx"
    export_negative_to_excel(db, out2)
    assert out2.exists(), "负面导出文件应存在"
    print(f"  OK - {out1.name}, {out2.name}")

    # 10. 备份
    print("\n[10] 数据库备份...")
    from src.export_utils import backup_db
    backup_path = backup_db(db, ROOT / "data" / "backups")
    assert Path(backup_path).exists(), "备份文件应存在"
    print(f"  OK - {backup_path}")

    # 11. 通知模块（不实际发送，只验证导入）
    print("\n[11] 通知模块...")
    from src import notify
    # 空 URL 不应报错，返回 False
    assert notify.send_dingtalk("", "t", "x") is False
    assert notify.send_wechat("", "t", "x") is False
    print("  OK - 接口正常")

    print("\n" + "=" * 50)
    print("全部测试通过 ✓")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
