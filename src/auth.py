"""
用户认证模块
- 内置管理员账户
- 仅管理员可添加普通用户
"""
import hashlib
import aiosqlite
from pathlib import Path
from typing import Optional


def _hash_pwd(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


async def init_auth_db(db_path: Path) -> None:
    """初始化用户表，创建默认管理员"""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # 默认管理员 admin / admin123（首次运行后建议修改）
        default_hash = _hash_pwd("admin123")
        try:
            await db.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                ("admin", default_hash, "admin"),
            )
        except aiosqlite.IntegrityError:
            pass
        await db.commit()


async def login(db_path: Path, username: str, password: str) -> Optional[dict]:
    """登录验证，成功返回用户信息"""
    pwd_hash = _hash_pwd(password)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, username, role FROM users WHERE username = ? AND password_hash = ?",
            (username, pwd_hash),
        )
        row = await cur.fetchone()
    if row:
        return {"id": row["id"], "username": row["username"], "role": row["role"]}
    return None


async def add_user(
    db_path: Path, username: str, password: str, role: str = "user"
) -> tuple[bool, str]:
    """添加用户（仅管理员可调用）"""
    if role not in ("user", "admin"):
        return False, "无效角色"
    pwd_hash = _hash_pwd(password)
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                (username, pwd_hash, role),
            )
            await db.commit()
        return True, "添加成功"
    except aiosqlite.IntegrityError:
        return False, "用户名已存在"


async def change_password(db_path: Path, username: str, new_password: str) -> bool:
    """修改密码（管理员可修改任意用户）"""
    pwd_hash = _hash_pwd(new_password)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (pwd_hash, username),
        )
        await db.commit()
    return True


async def list_users(db_path: Path) -> list[dict]:
    """列出所有用户（仅管理员）"""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id, username, role FROM users")
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


def can_modify_db(role: str) -> bool:
    """普通用户不可更改/删除数据库"""
    return role == "admin"
