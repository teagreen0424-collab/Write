"""记忆写入：章节正文切片嵌入 + 构思注入记录。"""
from dataclasses import dataclass, field

from app.infrastructure import repository as repos
from app.infrastructure import models, vector


@dataclass
class MemoryWriteResult:
    """区分无正文、嵌入降级与写库失败，不把零片段一律当作成功。"""

    written: int
    total: int
    failed: int
    status: str
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {name: getattr(self, name) for name in ("written", "total", "failed", "status")}


def _delete_chapter_slices(db, novel_id: str, chapter_id: str):
    db.query(models.MemoryChunk).filter(
        models.MemoryChunk.novel_id == novel_id,
        models.MemoryChunk.kind == "chapter_slice",
        models.MemoryChunk.source_chapter_id == chapter_id,
    ).delete(synchronize_session=False)


def write_chapter_memory(db, novel_id: str, chapter_id: str, content: str) -> MemoryWriteResult:
    """原子替换同章当前记忆；嵌入失败的切片降级省略，旧稿仍需失效。"""
    repos.ChapterRepo(db).get(novel_id, chapter_id)
    texts = _slice_content(content)
    chunks = []
    if texts:
        try:
            service = vector.EmbeddingService()
        except Exception:  # noqa: BLE001  初始化失败也按嵌入不可用降级
            service = None
        if service is not None:
            for text in texts:
                try:
                    embedding = service.embed([text])[0]
                    if not embedding:
                        raise ValueError("嵌入结果为空")
                    chunks.append(models.MemoryChunk(
                        novel_id=novel_id, text=text, vector=embedding,
                        kind="chapter_slice", source_chapter_id=chapter_id,
                    ))
                except Exception:  # noqa: BLE001  嵌入失败降级，忽略本次切片
                    continue
    try:
        _delete_chapter_slices(db, novel_id, chapter_id)
        db.add_all(chunks)
        db.commit()
    except Exception:  # noqa: BLE001  数据库失败回滚整个替换，不能留下半套切片
        db.rollback()
        # 不留下部分新记忆；另起事务仅让旧稿失效。持续写库故障时，召回层还会核对当前正文。
        try:
            _delete_chapter_slices(db, novel_id, chapter_id)
            db.commit()
            warning = "章节已定稿，但记忆写入失败；该章旧记忆已失效，请稍后重试定稿沉淀。"
        except Exception:  # noqa: BLE001
            db.rollback()
            warning = "章节已定稿，但记忆写入及旧记忆清理失败；召回会排除不匹配当前正文的片段，请稍后重试定稿沉淀。"
        return MemoryWriteResult(0, len(texts), len(texts), "storage_failed", [warning])

    failed = len(texts) - len(chunks)
    if not texts:
        return MemoryWriteResult(0, 0, 0, "empty")
    if failed:
        if chunks:
            warning = f"章节已定稿，记忆仅写入 {len(chunks)}/{len(texts)} 个片段；部分嵌入失败，旧记忆已失效，请稍后重试定稿沉淀。"
            return MemoryWriteResult(len(chunks), len(texts), failed, "partial", [warning])
        warning = "章节已定稿，但嵌入服务未生成可用记忆；该章旧记忆已失效，请稍后重试定稿沉淀。"
        return MemoryWriteResult(0, len(texts), failed, "embedding_failed", [warning])
    return MemoryWriteResult(len(chunks), len(texts), 0, "saved")


def record_injection(
    db,
    novel_id: str,
    channel: str,
    raw_text: str,
    parsed_intent: dict | None = None,
    applied_changes: dict | None = None,
):
    """写一条构思注入记录。"""
    return repos.InjectionRepo(db).create(
        novel_id,
        channel=channel,
        raw_text=raw_text or "",
        parsed_intent=parsed_intent or {},
        applied_changes=applied_changes or {},
    )


def _slice_content(content: str, max_len: int = 500) -> list[str]:
    """按段落聚合，切成不超过 max_len 的切片。"""
    if not content:
        return []
    paragraphs = [p.strip() for p in content.split("\n") if p.strip()]
    if not paragraphs:
        paragraphs = [content.strip()]

    slices: list[str] = []
    buf = ""
    for p in paragraphs:
        if len(buf) + len(p) + 1 <= max_len:
            buf = (buf + "\n" + p).strip()
        else:
            if buf:
                slices.append(buf)
            buf = p
    if buf:
        slices.append(buf)
    return slices
