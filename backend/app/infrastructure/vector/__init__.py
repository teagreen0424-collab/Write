"""向量检索层：SQLite 存向量 + NumPy 余弦相似度。"""
import numpy as np

from app.infrastructure import repository as repos
from app.infrastructure import models
from app.infrastructure.ai.embedding import EmbeddingService


def store(
    db,
    novel_id: str,
    text: str,
    kind: str = "chapter_slice",
    source_chapter_id: str = "",
    embedding_id: str = "",
) -> str:
    """嵌入文本并落库为 memory_chunk，返回 chunk id。失败抛异常由调用方降级。"""
    service = EmbeddingService()
    vector = service.embed([text])[0]
    chunk = repos.MemoryChunkRepo(db).create(
        novel_id,
        kind=kind,
        source_chapter_id=source_chapter_id,
        text=text,
        embedding_id=embedding_id,
        vector=vector,
    )
    return chunk.id


def search(db, novel_id: str, query: str, top_k: int = 3) -> list[dict]:
    """按余弦相似度召回 top-k 相似片段。失败抛异常由调用方降级。"""
    if not query:
        return []
    chunks = [c for c in repos.MemoryChunkRepo(db).list_by_novel(novel_id) if c.vector]
    # 写库故障可能保留已替换正文的旧切片。按候选章节批量核对，设定与无来源旧数据保持兼容。
    chapter_ids = {c.source_chapter_id for c in chunks if c.kind == "chapter_slice" and c.source_chapter_id}
    if chapter_ids:
        current = {
            chapter.id: "\n".join(line.strip() for line in (chapter.content or "").split("\n") if line.strip())
            for chapter in db.query(models.Chapter.id, models.Chapter.content).filter(
                models.Chapter.novel_id == novel_id,
                models.Chapter.id.in_(chapter_ids),
            ).all()
        }
        chunks = [c for c in chunks if c.kind != "chapter_slice" or not c.source_chapter_id
                  or (c.text and c.text in current.get(c.source_chapter_id, ""))]
    if not chunks:
        return []  # 无记忆片段，避免无谓的嵌入

    service = EmbeddingService()
    query_vec = service.embed([query])[0]

    scored = []
    for c in chunks:
        scored.append(
            {
                "id": c.id,
                "text": c.text,
                "kind": c.kind,
                "source_chapter_id": c.source_chapter_id,
                "embedding_id": c.embedding_id,
                "score": _cosine(query_vec, c.vector),
            }
        )
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


def _cosine(a, b) -> float:
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if a.shape[0] == 0 or b.shape[0] == 0 or a.shape != b.shape:
        return 0.0
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)
