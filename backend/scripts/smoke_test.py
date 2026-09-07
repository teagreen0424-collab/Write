"""阶段零验收脚本：验证本地嵌入 + ChromaDB 读写 + LLM 补全。

用法（在 backend 目录下）：
    python -m scripts.smoke_test
"""
from infrastructure.ai.embedding_service import EmbeddingService
from infrastructure.ai.llm_provider import LLMProvider
from infrastructure.ai.prompts import SCAFFOLD_SMOKE_PROMPT
from infrastructure.vector.chroma_store import VectorStore


def test_embedding_and_chroma() -> None:
    service = EmbeddingService()
    store = VectorStore()

    text = "主角林越在青云山下捡到一枚神秘玉佩。"
    vectors = service.embed([text])
    print(f"[Embedding] 向量维度：{len(vectors[0])}")

    store.upsert(
        ids=["smoke-1"],
        embeddings=vectors,
        documents=[text],
        metadatas=[{"kind": "chapter_slice"}],
    )
    result = store.query(query_embeddings=vectors, n_results=1)
    docs = result.get("documents")
    print(f"[ChromaDB] 读回：{docs}")
    assert docs and docs[0] and docs[0][0] == text, "ChromaDB 读回内容不一致"


def test_llm() -> str:
    provider = LLMProvider()
    text = provider.complete([{"role": "user", "content": SCAFFOLD_SMOKE_PROMPT}])
    print(f"[LLM] 返回：{text}")
    return text


if __name__ == "__main__":
    test_embedding_and_chroma()
    test_llm()
    print("阶段零验收通过")
