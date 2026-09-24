"""全局对话用例：reply_conversation + build_chat_context + promote。"""
import json

from sqlalchemy.orm import Session

from app.infrastructure import repository as repos
from app.infrastructure.ai.llm_provider import LLMProvider
from app.infrastructure.ai.prompts import CHAT_ADVISOR_SYSTEM

llm = LLMProvider()

_LEVEL_LABEL = {"volume": "卷", "act": "幕", "chapter": "章"}


def sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def build_chat_context(
    db: Session, novel_id: str, max_chars: int = 2000, query: str = "",
) -> str:
    """按人物→历史召回→世界观→大纲→近期摘要的顺序装配，不整节丢弃。"""
    sections: list[tuple[str, list[str]]] = []

    char_lines = [
        f"{c.name}（{c.role}）— {c.personality} — {c.goal}"
        for c in repos.CharacterRepo(db).list(novel_id)
    ]
    if char_lines:
        sections.append(("【人物·当前设定】", char_lines))

    if query.strip():
        try:
            from app.infrastructure.vector import search

            hits = search(db, novel_id, query, top_k=3)
        except Exception:  # noqa: BLE001  检索失败只降级对话上下文
            hits = []
        seen: set[str] = set()
        recall_lines = []
        for hit in hits:
            text = str(hit.get("text") or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            source = "当前设定索引" if hit.get("kind") == "setting" else "历史章节片段"
            recall_lines.append(f"[{source}，以 Story Bible 为准] {text}")
        if recall_lines:
            sections.append(("【语义召回·仅作参考】", recall_lines))

    ws_lines = [
        f"{w.name} [{w.category}] — {w.description}"
        for w in repos.WorldSettingRepo(db).list(novel_id)
    ]
    if ws_lines:
        sections.append(("【世界观·当前设定】", ws_lines))

    outline_lines = [
        f"{_LEVEL_LABEL.get(n.level, n.level)}·{n.title}"
        for n in repos.OutlineRepo(db).list_all(novel_id)
    ]
    if outline_lines:
        sections.append(("【大纲】", outline_lines))

    chain_lines = [
        f"第{c.seq}章：{c.summary}"
        for c in repos.ChapterRepo(db).finalized(novel_id)[-3:]
        if c.summary
    ]
    if chain_lines:
        sections.append(("【最近章节】", chain_lines))

    result = ""
    for heading, lines in sections:
        first_line = True
        for line in lines:
            prefix = ("\n\n" + heading + "\n") if first_line else "\n"
            room = max_chars - len(result) - len(prefix)
            if room <= 0:
                return result
            piece = line[: min(room, 400)]
            result += prefix + piece
            first_line = False
            if len(piece) < min(len(line), 400):
                return result
    return result


def reply_conversation(db: Session, conversation_id: str, content: str):
    """收到用户消息 → 落库 → 装配上下文 → 流式回复 → 落库 assistant 消息。"""
    try:
        repo = repos.ConversationRepo(db)
        conv = repo.get(conversation_id)

        user_msg = repo.add_message(conversation_id, "user", content)
        # 首次消息后自动命名（已有标题不覆盖）
        if not conv.title:
            repo.update(conversation_id, title=content.strip()[:20])
            conv = repo.get(conversation_id)

        history = repo.list_messages(conversation_id)
        recent = history[-20:]
        messages: list[dict] = [{"role": "system", "content": CHAT_ADVISOR_SYSTEM}]
        if conv.novel_id:
            ctx = build_chat_context(db, conv.novel_id, query=content)
            if ctx:
                messages.append({"role": "system", "content": "当前作品设定：\n" + ctx})
        for m in recent:
            messages.append({"role": m.role, "content": m.content})

        full = ""
        for chunk in llm.stream_complete(messages):
            full += chunk
            yield sse({"type": "token", "content": chunk})

        assistant_msg = repo.add_message(conversation_id, "assistant", full)
        yield sse(
            {
                "type": "done",
                "user_message_id": user_msg.id,
                "assistant_message_id": assistant_msg.id,
                "title": conv.title,
            }
        )
    except Exception as exc:  # noqa: BLE001
        yield sse({"type": "error", "detail": str(exc)})


def promote_conversation(
    db: Session, conversation_id: str, title: str, genre: str = "", summary: str = ""
):
    """全局会话转为作品：建 novel 并把会话归组，返回新作品。"""
    repos.ConversationRepo(db).get(conversation_id)
    novel = repos.NovelRepo(db).create(title=title, genre=genre, summary=summary)
    repos.ConversationRepo(db).update(conversation_id, novel_id=novel.id)
    return novel
