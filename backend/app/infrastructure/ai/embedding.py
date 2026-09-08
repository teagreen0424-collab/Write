"""嵌入服务：本地 fastembed（bge-small-zh-v1.5），可切云端 OpenAI 兼容嵌入。

嵌入失败会抛出异常，由调用方降级处理。
"""
import httpx

from app.config import get_settings

DEFAULT_LOCAL_MODEL = "BAAI/bge-small-zh-v1.5"


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

            self._local_model = TextEmbedding(model_name=self._model_path)
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
        return [item["embedding"] for item in data["data"]]
