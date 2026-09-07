"""数据库连接与会话管理。"""
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings

settings = get_settings()


def _resolve_sqlite_path(url: str) -> str:
    """将 sqlite 相对路径解析为 backend 目录下的绝对路径，并确保目录存在。"""
    if url.startswith("sqlite:///"):
        raw = url[len("sqlite:///") :]
        p = Path(raw)
        if not p.is_absolute():
            p = Path(__file__).resolve().parents[2] / p
        p.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{p.as_posix()}"
    return url


db_url = _resolve_sqlite_path(settings.database_url)

connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}

engine = create_engine(db_url, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db():
    """FastAPI 依赖：请求级数据库会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """创建全部表。阶段一用 create_all，后续引入迁移工具后替换。"""
    from app.infrastructure import models  # noqa: F401  # 注册模型

    models.Base.metadata.create_all(bind=engine)
