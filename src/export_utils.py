"""
导出与备份
"""
import shutil
import sqlite3
from pathlib import Path
from datetime import datetime
import pandas as pd


def export_collection_to_excel(db_path: Path, out_path: Path) -> str:
    """采集数据导出 Excel"""
    with sqlite3.connect(db_path) as db:
        df = pd.read_sql_query(
            "SELECT platform, item_id, nickname, content, link, publish_date, created_at FROM collection",
            db,
        )
    df.to_excel(out_path, index=False, engine="openpyxl")
    return str(out_path)


def export_negative_to_excel(db_path: Path, out_path: Path) -> str:
    """负面言论导出 Excel"""
    with sqlite3.connect(db_path) as db:
        df = pd.read_sql_query(
            "SELECT platform, item_id, nickname, content, link, publish_date, sentiment, remark, created_at FROM negative",
            db,
        )
    df.to_excel(out_path, index=False, engine="openpyxl")
    return str(out_path)


def export_watched_to_excel(db_path: Path, out_path: Path) -> str:
    """关注对象数据导出 Excel"""
    with sqlite3.connect(db_path) as db:
        df = pd.read_sql_query(
            "SELECT platform, item_id, nickname, content, link, publish_date, watch_target_id, watch_target_name, created_at FROM watched",
            db,
        )
    df.to_excel(out_path, index=False, engine="openpyxl")
    return str(out_path)


def backup_db(db_path: Path, backup_dir: Path) -> str:
    """数据库备份"""
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = backup_dir / f"backup_{ts}.db"
    shutil.copy2(db_path, dest)
    return str(dest)
