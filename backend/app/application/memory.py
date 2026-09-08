"""记忆写入：章节正文切片嵌入 + 构思注入记录。"""
from app.infrastructure import repository as repos
from app.infrastructure.vector import store as vector_store


def write_chapter_memory(db, novel_id: str, chapter_id: str, content: str) -> int:
    """把章节正文切片嵌入后写 memory_chunk（失败可忽略，不阻塞主流程）。"""
    count = 0
    for text in _slice_content(content):
        try:
            vector_store(
                db,
                novel_id,
                text,
                kind="chapter_slice",
                source_chapter_id=chapter_id,
            )
            count += 1
        except Exception:  # noqa: BLE001  嵌入失败降级，忽略本次切片
            continue
    return count


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
