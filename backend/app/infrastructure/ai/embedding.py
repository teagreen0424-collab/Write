"""嵌入服务：本地 fastembed（bge-small-zh-v1.5），可切云端 OpenAI 兼容嵌入。

嵌入失败会抛出异常，由调用方降级处理。
"""
import httpx
from pathlib import Path

from app.config import get_settings

DEFAULT_LOCAL_MODEL = "BAAI/bge-small-zh-v1.5"


def _default_cache_dir() -> Path:
    """本地模型缓存目录（项目内固定路径，避免系统 Temp 被清理）。"""
    # embedding.py 位于 backend/app/infrastructure/ai/，往上三级到 backend/
    return Path(__file__).resolve().parents[3] / "fastembed_cache"


class EmbeddingService:
    """本地/云端嵌入统一入口，懒加载本地模型。"""

    def __init__(self) -> None:
        settings = get_settings()
        self._service = settings.embedding_service or "local"
        self._api_key = settings.embedding_api_key
        self._base_url = (settings.embedding_base_url or "").rstrip("/")
        self._model = settings.embedding_model or ""
        self._model_path = settings.embedding_model_path or DEFAULT_LOCAL_MODEL
        self._local_model = None

    def _ensure_local(self):
        if self._local_model is None:
            from fastembed import TextEmbedding

            self._local_model = TextEmbedding(
                model_name=self._model_path,
                cache_dir=str(_default_cache_dir()),
            )
        return self._local_model

    def embed(self, texts: list[str]) -> list[list[float]]:
        """把文本编码为向量列表，失败抛异常。"""
        if not texts:
            return []
        if self._service == "openai" and self._base_url and self._api_key:
            return self._embed_cloud(texts)
        return self._embed_local(texts)

    def _embed_local(self, texts: list[str]) -> list[list[float]]:
        model = self._ensure_local()
        vectors = list(model.embed(texts))
        return [list(map(float, v)) for v in vectors]

    def _embed_cloud(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = httpx.post(
            f"{self._base_url}/embeddings",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={"model": self._model, "input": texts},
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list) or len(items) != len(texts):
            raise ValueError("嵌入数量与输入不一致，无法可靠对应")
        if any(not isinstance(item, dict) or "embedding" not in item for item in items):
            raise ValueError("嵌入响应缺少有效的向量记录")

        indexed = ["index" in item for item in items]
        # 旧兼容服务全部省略 index 时维持位置合同；混合响应不能猜对应。
        if not any(indexed):
            return [item["embedding"] for item in items]
        if not all(indexed):
            raise ValueError("嵌入索引不完整，无法可靠对应")
        indices = [item["index"] for item in items]
        if any(type(index) is not int or index < 0 or index >= len(texts) for index in indices):
            raise ValueError("嵌入索引必须是输入范围内的整数")
        if len(set(indices)) != len(texts):
            raise ValueError("嵌入索引重复或缺失，无法可靠对应")
        return [item["embedding"] for item in sorted(items, key=lambda item: item["index"])]
