"""SQLite 连接管理。单写者串行调度在后续阶段落地。"""
import sqlite3

from config import get_settings


def init_db() -> None:
    """初始化数据目录与数据库文件，验证可连接。"""
    settings = get_settings()
    settings.resolved_data_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(settings.sqlite_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("SELECT 1;")
    finally:
        conn.close()


def get_connection() -> sqlite3.Connection:
    """返回一个新的 SQLite 连接（阶段零仅验证连通，表结构在阶段一建立）。"""
    settings = get_settings()
    conn = sqlite3.connect(settings.sqlite_path)
    conn.row_factory = sqlite3.Row
    return conn
