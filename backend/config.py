"""全局配置：从 .env 读取，统一在 backend/config.py 定义。"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    """应用配置。所有敏感信息（API Key 等）只从 .env 读取，禁止提交。"""

    # LLM（OpenAI 兼容）
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"

    # 嵌入
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_device: str = "cpu"

    # 存储
    data_dir: str = str(BACKEND_DIR / "data")

    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def resolved_data_dir(self) -> Path:
        """数据目录：相对路径按 backend 目录解析。"""
        p = Path(self.data_dir)
        return p if p.is_absolute() else BACKEND_DIR / p

    @property
    def sqlite_path(self) -> str:
        return str(self.resolved_data_dir / "lingmo.db")

    @property
    def chroma_path(self) -> str:
        return str(self.resolved_data_dir / "chroma")


@lru_cache
def get_settings() -> Settings:
    return Settings()
