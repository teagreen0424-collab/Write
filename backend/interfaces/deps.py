"""依赖注入：提供各基础设施组件的惰性单例。"""
from functools import lru_cache

from infrastructure.ai.embedding_service import EmbeddingService
from infrastructure.ai.llm_provider import LLMProvider
from infrastructure.vector.chroma_store import VectorStore


@lru_cache
def get_llm_provider() -> LLMProvider:
    return LLMProvider()


@lru_cache
def get_embedding_service() -> EmbeddingService:
    return EmbeddingService()


@lru_cache
def get_vector_store() -> VectorStore:
    return VectorStore()
