"""
数据库模块 - SQLite 本地存储，带去重
"""
import aiosqlite
import hashlib
import json
from pathlib import Path
from typing import Optional
from datetime import datetime

PLATFORMS = ["抖音", "快手", "小红书", "微信视频号"]


def _dedup_key(platform: str, item_id: str, content_hash: str) -> str:
    """生成去重唯一键"""
    raw = f"{platform}|{item_id}|{content_hash}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def init_db(db_path: Path) -> None:
    """初始化所有数据库表"""
    async with aiosqlite.connect(db_path) as db:
        # 采集数据库（必做功能）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS collection (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                item_id TEXT NOT NULL,
                nickname TEXT,
                content TEXT,
                link TEXT,
                publish_date TEXT,
                content_hash TEXT,
                dedup_key TEXT UNIQUE,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_collection_platform ON collection(platform)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_collection_dedup ON collection(dedup_key)"
        )

        # 负面言论数据库
        await db.execute("""
            CREATE TABLE IF NOT EXISTS negative (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection_id INTEGER,
                platform TEXT,
                item_id TEXT,
                nickname TEXT,
                content TEXT,
                link TEXT,
                publish_date TEXT,
                sentiment TEXT,
                remark TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (collection_id) REFERENCES collection(id)
            )
        """)

        # 关注对象数据库
        await db.execute("""
            CREATE TABLE IF NOT EXISTS watched (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection_id INTEGER,
                platform TEXT,
                item_id TEXT,
                nickname TEXT,
                content TEXT,
                link TEXT,
                publish_date TEXT,
                watch_target_id TEXT,
                watch_target_name TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (collection_id) REFERENCES collection(id)
            )
        """)

        # 关注对象配置表（员工添加的关注对象）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS watch_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                target_id TEXT NOT NULL,
                target_name TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(platform, target_id)
            )
        """)

        await db.commit()


async def insert_collection(
    db_path: Path,
    platform: str,
    item_id: str,
    nickname: str,
    content: str,
    link: str,
    publish_date: str,
) -> bool:
    """插入采集数据，带去重。返回 True 表示新插入，False 表示重复跳过"""
    content_hash = hashlib.sha256(content.encode(errors="ignore")).hexdigest()
    dedup = _dedup_key(platform, item_id, content_hash)
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """INSERT INTO collection 
                   (platform, item_id, nickname, content, link, publish_date, content_hash, dedup_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (platform, item_id, nickname, content, link, publish_date, content_hash, dedup),
            )
            await db.commit()
        return True
    except aiosqlite.IntegrityError:
        return False  # UNIQUE 冲突，已存在


async def get_collection_batch(
    db_path: Path,
    platforms: Optional[list[str]] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """分批获取采集数据（用于语义判断）"""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        if platforms:
            placeholders = ",".join("?" * len(platforms))
            cur = await db.execute(
                f"""SELECT id, platform, item_id, nickname, content, link, publish_date 
                    FROM collection 
                    WHERE platform IN ({placeholders})
                    ORDER BY id LIMIT ? OFFSET ?""",
                (*platforms, limit, offset),
            )
        else:
            cur = await db.execute(
                """SELECT id, platform, item_id, nickname, content, link, publish_date 
                   FROM collection ORDER BY id LIMIT ? OFFSET ?""",
                (limit, offset),
            )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def insert_negative(db_path: Path, row: dict) -> None:
    """插入负面言论"""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO negative 
               (collection_id, platform, item_id, nickname, content, link, publish_date, sentiment, remark)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row.get("collection_id"),
                row.get("platform"),
                row.get("item_id"),
                row.get("nickname"),
                row.get("content"),
                row.get("link"),
                row.get("publish_date"),
                row.get("sentiment", "负面"),
                row.get("remark", ""),
            ),
        )
        await db.commit()


async def insert_watched(db_path: Path, row: dict) -> None:
    """插入关注对象发布的内容"""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO watched 
               (collection_id, platform, item_id, nickname, content, link, publish_date, watch_target_id, watch_target_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row.get("collection_id"),
                row.get("platform"),
                row.get("item_id"),
                row.get("nickname"),
                row.get("content"),
                row.get("link"),
                row.get("publish_date"),
                row.get("watch_target_id"),
                row.get("watch_target_name"),
            ),
        )
        await db.commit()


async def get_watch_config(db_path: Path) -> list[dict]:
    """获取关注对象配置"""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT platform, target_id, target_name FROM watch_config"
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def add_watch_config(db_path: Path, platform: str, target_id: str, target_name: str = "") -> bool:
    """添加关注对象"""
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO watch_config (platform, target_id, target_name) VALUES (?, ?, ?)",
                (platform, target_id, target_name or target_id),
            )
            await db.commit()
        return True
    except Exception:
        return False


async def delete_watch_config(db_path: Path, platform: str, target_id: str) -> bool:
    """删除关注对象"""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "DELETE FROM watch_config WHERE platform = ? AND target_id = ?",
            (platform, target_id),
        )
        await db.commit()
    return True


async def watched_collection_ids(db_path: Path) -> set[int]:
    """已存入关注对象数据库的 collection_id 集合"""
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT collection_id FROM watched WHERE collection_id IS NOT NULL")
        rows = await cur.fetchall()
    return {r[0] for r in rows}


async def get_collection_by_ids(db_path: Path, ids: list[int]) -> list[dict]:
    """根据 collection id 列表获取记录"""
    if not ids:
        return []
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" * len(ids))
        cur = await db.execute(
            f"SELECT * FROM collection WHERE id IN ({placeholders})",
            ids,
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]
