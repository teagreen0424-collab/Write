"""嵌入服务：本地 bge-small-zh-v1.5，可切云端 OpenAI 兼容嵌入。"""
from sentence_transformers import SentenceTransformer

from config import get_settings


class EmbeddingService:
    """本地嵌入服务封装（懒加载，避免启动即下载模型）。"""

    def __init__(self) -> None:
        settings = get_settings()
        self._model_name = settings.embedding_model
        self._device = settings.embedding_device
        self._model: SentenceTransformer | None = None

    def _ensure_model(self) -> SentenceTransformer:
        if self._model is None:
            self._model = SentenceTransformer(self._model_name, device=self._device)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        """把文本编码为归一化向量。"""
        model = self._ensure_model()
        vectors = model.encode(texts, normalize_embeddings=True)
        return vectors.tolist()
