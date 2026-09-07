"""ChromaDB 本地持久化向量库封装。"""
import chromadb

from config import get_settings


class VectorStore:
    """本地向量库封装，内部按 collection 名缓存。"""

    def __init__(self) -> None:
        settings = get_settings()
        self._client = chromadb.PersistentClient(path=settings.chroma_path)
        self._collections: dict[str, object] = {}

    def get_collection(self, name: str = "memory"):
        if name not in self._collections:
            self._collections[name] = self._client.get_or_create_collection(name=name)
        return self._collections[name]

    def upsert(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict] | None = None,
        collection: str = "memory",
    ) -> None:
        self.get_collection(collection).upsert(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )

    def query(
        self,
        query_embeddings: list[list[float]],
        n_results: int = 5,
        collection: str = "memory",
    ) -> dict:
        return self.get_collection(collection).query(
            query_embeddings=query_embeddings,
            n_results=n_results,
        )
