"""生成新章的最终写事务：只串行来源核对与落库，不锁模型等待。"""
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import literal_column, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.application.chapter_creation import validate_outline_binding
from app.application.revisions import _begin_chapter_write, _freeze_chapter, _require_clean_session
from app.infrastructure import models
from app.infrastructure.repository import ChapterRepo


class GeneratedChapterConflict(HTTPException):
    hint = "已输出正文仅是待核对候选，可复制保留后明确重新生成"
    saved = False

    def __init__(self, detail: str = "生成期间最新章节发生变化或默认章序已改变；本次候选未保存，请保留输出并重新核对"):
        super().__init__(status_code=409, detail=detail)


@dataclass(frozen=True)
class GenerationSource:
    novel_id: str
    latest_id: str
    latest_seq: int | None
    next_seq: int


def require_generation_session(db: Session):
    """生成用例拥有独立会话；拒绝必须发生在任何读取/autoflush与rollback之前。"""
    try:
        _require_clean_session(db)
    except RuntimeError:
        raise GeneratedChapterConflict("生成需要没有未提交修改的独立会话；本次候选未保存，调用方修改保持不变") from None


def _ensure_novel(db: Session, novel_id: str):
    table = models.Novel.__table__
    if db.execute(select(table.c.id).where(table.c.id == novel_id)).first() is None:
        raise HTTPException(status_code=404, detail="作品不存在")


def capture_source(db: Session, novel_id: str) -> GenerationSource:
    """仅承诺真实最新身份/序号和默认下一序号，不声称整本设定/作者字段快照。"""
    _ensure_novel(db, novel_id)
    row = db.query(models.Chapter.id, models.Chapter.seq).filter(
        models.Chapter.novel_id == novel_id,
    ).order_by(models.Chapter.seq.desc(), literal_column("chapter.rowid").desc()).first()
    return GenerationSource(novel_id, row.id if row else "", row.seq if row else None,
                            ((row.seq or 0) if row else 0) + 1)


def _chat_source(db: Session, novel_id: str, message_id: str):
    message, conversation = models.Message.__table__, models.Conversation.__table__
    row = db.execute(select(message.c.id).join(conversation,
        message.c.conversation_id == conversation.c.id).where(
        message.c.id == message_id, message.c.role == "assistant", conversation.c.novel_id == novel_id,
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail="聊天来源已不存在或不属于当前作品")


def existing_chat_chapter(db: Session, novel_id: str, message_id: str) -> dict | None:
    """同消息已完成先回原结果，不能因后续新章或已删目标而盲目再生成。"""
    _ensure_novel(db, novel_id)
    _chat_source(db, novel_id, message_id)
    action = models.ChatAction.__table__
    previous = db.execute(select(action.c.novel_id, action.c.result_id).where(
        action.c.message_id == message_id, action.c.action_type == "chapter",
    )).first()
    if previous is None:
        return None
    if previous.novel_id != novel_id:
        raise GeneratedChapterConflict("原聊天动作属于其他作品，本次不会写入或重放")
    chapter = models.Chapter.__table__
    current = db.execute(select(chapter).where(
        chapter.c.novel_id == novel_id, chapter.c.id == previous.result_id,
    )).mappings().first()
    if current is None:
        raise HTTPException(status_code=404, detail="原聊天动作对应章节已删除，不会再次创建")
    return dict(current)


def save_generated_chapter(
    db: Session, novel_id: str, source: GenerationSource, fields: dict, *, action_message_id: str = "",
) -> dict:
    # try之外拒绝调用方DML；外层生成入口也不能rollback此拒绝。
    require_generation_session(db)
    try:
        _begin_chapter_write(db)
        if action_message_id:
            previous = existing_chat_chapter(db, novel_id, action_message_id)
            if previous is not None:
                db.rollback()  # 仅释放本函数的无DML短写锁，返回已冻结的真实原结果。
                return previous
        current = capture_source(db, novel_id)
        if current != source or source.novel_id != novel_id or fields.get("seq") != source.next_seq:
            raise GeneratedChapterConflict()
        try:
            validate_outline_binding(db, novel_id, fields.get("outline_node_id"))
        except HTTPException:
            raise GeneratedChapterConflict("生成期间所选章级大纲已不存在或不可绑定；本次候选未保存，请核对后重新生成") from None
        action = None
        if action_message_id:
            action = models.ChatAction(message_id=action_message_id, novel_id=novel_id,
                action_type="chapter", status="done", result_id="", result_summary={})
            db.add(action)
            db.flush()
        chapter = ChapterRepo(db).create(novel_id, commit=False, **fields)
        if action is not None:
            action.result_id = chapter.id
            action.result_summary = {"chapter_id": chapter.id, "title": chapter.title}
        result = _freeze_chapter(db, chapter)
        db.commit()
    except OperationalError:
        db.rollback()
        raise GeneratedChapterConflict("章节正在保存，本次候选未保存；请保留输出并核对目录后重试") from None
    except Exception:
        db.rollback()
        raise
    # 已提交之后只使用提交前冻结的全列；不能因refresh锁故障否认成功。
    return result
