"""全局对话用例：reply_conversation + build_chat_context + promote。"""
import hashlib
import json
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.infrastructure import repository as repos
from app.infrastructure import models
from app.infrastructure.ai.llm_provider import LLMProvider
from app.infrastructure.ai.prompts import CHAT_ADVISOR_SYSTEM

llm = LLMProvider()

_LEVEL_LABEL = {"volume": "卷", "act": "幕", "chapter": "章"}


class ChatMessageRequestError(HTTPException):
    def __init__(self, status_code: int, detail: str, code: str):
        super().__init__(status_code=status_code, detail=detail)
        self.code = code


def _request_uuid(request_id) -> str:
    try:
        return str(UUID(str(request_id)))
    except (TypeError, ValueError, AttributeError):
        raise ChatMessageRequestError(422, "发送请求身份必须是UUID", "chat_message_request_invalid") from None


def _ensure_conversation(db: Session, conversation_id: str):
    table = models.Conversation.__table__
    if db.execute(select(table.c.id).where(table.c.id == conversation_id)).first() is None:
        raise ChatMessageRequestError(404, "会话不存在，原消息不能在其它会话重发", "chat_conversation_not_found")


def _request_message(db: Session, request_id: str):
    table = models.Message.__table__
    return db.execute(select(table).where(table.c.id == request_id)).mappings().first()


def _message_receipt(row, conversation_id: str, *, replayed: bool, content: str | None = None) -> dict:
    if (row["conversation_id"] != conversation_id or row["role"] != "user"
            or not isinstance(row["content"], str) or (content is not None and row["content"] != content)):
        raise ChatMessageRequestError(409, "发送请求身份与原会话或原文不一致，请保留原稿核对", "chat_message_request_conflict")
    return {"request_id": row["id"], "conversation_id": conversation_id, "user_message_id": row["id"],
            "content": row["content"], "content_hash": hashlib.sha256(row["content"].encode("utf-8")).hexdigest(),
            "accepted": True, "receipt_replayed": replayed}


def get_message_receipt(db: Session, conversation_id: str, request_id) -> dict:
    """真实新读只确认原用户消息已存，不依赖可编辑meta或猜测助手回复。"""
    request_id = _request_uuid(request_id)
    with db.no_autoflush:
        _ensure_conversation(db, conversation_id)
        row = _request_message(db, request_id)
        if row is None:
            raise ChatMessageRequestError(404, "尚未找到该次发送的保存记录", "chat_message_request_not_found")
        return _message_receipt(row, conversation_id, replayed=True)


def _require_request_session(db: Session):
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("发送确认必须使用没有未提交修改的请求专用会话")
    if db.in_transaction() and db.get_bind().dialect.name == "sqlite":
        if db.connection().connection.driver_connection.in_transaction:
            raise RuntimeError("发送确认不能提交调用方已执行的写事务")


def _accept_message_request(db: Session, conversation_id: str, content: str, request_id: str) -> dict:
    # 拒绝在try外，不autoflush或rollback调用方的章节/设定事务。
    _require_request_session(db)
    with db.no_autoflush:
        _ensure_conversation(db, conversation_id)
        previous = _request_message(db, request_id)
        if previous is not None:
            return _message_receipt(previous, conversation_id, replayed=True, content=content)
    try:
        # 此处仅有干净的只读autobegin；锁只覆盖查重、原文与首次标题保存。
        db.rollback()
        if db.get_bind().dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))
        db.expire_all()
        _ensure_conversation(db, conversation_id)
        previous = _request_message(db, request_id)
        if previous is not None:
            receipt = _message_receipt(previous, conversation_id, replayed=True, content=content)
            db.rollback()  # 释放本函数拥有的短事务，历史重放没有任何写入。
            return receipt
        repo = repos.ConversationRepo(db)
        conv = repo.get(conversation_id)
        if not conv.title:
            conv.title = content.strip()[:20]
        message = repo.add_message(conversation_id, "user", content, message_id=request_id, commit=False)
        frozen = {column.key: getattr(message, column.key) for column in models.Message.__table__.columns}
        receipt = _message_receipt(frozen, conversation_id, replayed=False)
        db.commit()
        return receipt
    except Exception:
        db.rollback()
        raise


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


def reply_conversation(db: Session, conversation_id: str, content: str, *, request_id=None):
    """收到用户消息 → 落库 → 装配上下文 → 流式回复 → 落库 assistant 消息。"""
    try:
        repo = repos.ConversationRepo(db)
        if request_id is not None:
            request_id = _request_uuid(request_id)
            receipt = _accept_message_request(db, conversation_id, content, request_id)
            if receipt["receipt_replayed"]:
                yield sse({"type": "receipt", **receipt, "reply_restarted": False})
                return
            user_message_id = receipt["user_message_id"]
            yield sse({"type": "user_saved", **receipt})
            conv = repo.get(conversation_id)
        else:
            conv = repo.get(conversation_id)
            user_msg = repo.add_message(conversation_id, "user", content)
            user_message_id = user_msg.id  # 下一次commit会expire其它对象，必须先冻结原ID。
            # 无请求身份的旧调用保留原命名与token/done合同。
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

        # 在提交助手消息前冻结标题；done不能因读取expired用户/会话而否认已保存结果。
        title = db.execute(select(models.Conversation.__table__.c.title).where(
            models.Conversation.__table__.c.id == conversation_id,
        )).scalar_one_or_none()
        assistant_msg = repo.add_message(conversation_id, "assistant", full)
        extra = {"request_id": request_id, "conversation_id": conversation_id} if request_id is not None else {}
        yield sse(
            {
                "type": "done",
                "user_message_id": user_message_id,
                "assistant_message_id": assistant_msg.id,
                "title": title,
                **extra,
            }
        )
    except Exception as exc:  # noqa: BLE001
        extra = {"code": getattr(exc, "code", "chat_generation_failed"),
                 "status": getattr(exc, "status_code", 500)} if request_id is not None else {}
        if request_id is not None and isinstance(exc, SQLAlchemyError):
            detail = "本次消息保存或读取未完成，请保留原稿并核对原发送；回复是否完整请核对历史消息"
            extra = {"code": "chat_generation_failed", "status": 500}
        else:
            detail = getattr(exc, "detail", str(exc))
        yield sse({"type": "error", "detail": detail, **extra})


def promote_conversation(
    db: Session, conversation_id: str, title: str, genre: str = "", summary: str = ""
):
    """全局会话转为作品：建 novel 并把会话归组，返回新作品。"""
    repos.ConversationRepo(db).get(conversation_id)
    novel = repos.NovelRepo(db).create(title=title, genre=genre, summary=summary)
    repos.ConversationRepo(db).update(conversation_id, novel_id=novel.id)
    return novel
