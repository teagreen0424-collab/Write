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


def build_chat_context(db: Session, novel_id: str, max_chars: int = 2000) -> str:
    """轻量装配作品上下文（区别于章节生成的全量 build_context，控制 token）。

    保留优先级（从后往前丢弃）：摘要链 > 大纲 > 世界观 > 人物卡。
    """
    sections: list[str] = []

    char_lines = [
        f"{c.name}（{c.role}）— {c.personality} — {c.goal}"
        for c in repos.CharacterRepo(db).list(novel_id)
    ]
    if char_lines:
        sections.append("【人物】\n" + "\n".join(char_lines))

    ws_lines = [
        f"{w.name} [{w.category}] — {w.description}"
        for w in repos.WorldSettingRepo(db).list(novel_id)
    ]
    if ws_lines:
        sections.append("【世界观】\n" + "\n".join(ws_lines))

    outline_lines = [
        f"{_LEVEL_LABEL.get(n.level, n.level)}·{n.title}"
        for n in repos.OutlineRepo(db).list_all(novel_id)
    ]
    if outline_lines:
        sections.append("【大纲】\n" + "、".join(outline_lines))

    chain_lines = [
        f"第{c.seq}章：{c.summary}"
        for c in repos.ChapterRepo(db).finalized(novel_id)[-3:]
        if c.summary
    ]
    if chain_lines:
        sections.append("【最近章节】\n" + "\n".join(chain_lines))

    text = "\n\n".join(sections)
    if len(text) <= max_chars:
        return text
    # 从后往前丢弃（先丢摘要链）
    while sections and len("\n\n".join(sections)) > max_chars:
        sections.pop()
    return "\n\n".join(sections)


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
            ctx = build_chat_context(db, conv.novel_id)
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
