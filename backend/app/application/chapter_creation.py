"""手工新章创建：请求身份、默认序号和正文在短写事务内原子提交。"""
import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.application.revisions import AUTHOR_FIELDS, _begin_chapter_write, _require_clean_session
from app.infrastructure import models
from app.infrastructure.repository import ChapterRepo, NovelRepo, OutlineRepo
from app.interfaces.schemas import ChapterCreate


class ChapterCreationError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = 409


def validate_outline_binding(db: Session, novel_id: str, node_id: str | None):
    """空绑定合法；新的绑定必须是当前作品中仍存在的章级大纲。"""
    if not node_id:
        return
    try:
        node = OutlineRepo(db).get(novel_id, node_id)
    except HTTPException as error:
        raise HTTPException(status_code=400, detail="大纲节点不存在或不属于当前作品") from error
    if node.level != "chapter":
        raise HTTPException(status_code=400, detail="章节只能绑定章级大纲节点")


def _fingerprint(novel_id: str, payload: dict) -> str:
    canonical = json.dumps({"version": 1, "novel_id": novel_id, "payload": payload},
                           ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _receipt(db: Session, request_id: str):
    # 列投影绕过身份缓存，迟到重放不能读到其它请求修改前的章/记录。
    table = models.ChapterCreationReceipt.__table__
    return db.execute(select(table).where(table.c.request_id == request_id)).mappings().first()


def _replay(db: Session, novel_id: str, receipt, fingerprint: str, payload: dict) -> dict:
    if receipt["novel_id"] != novel_id:
        raise ChapterCreationError("chapter_creation_scope_conflict", "该创建请求已用于另一作品，请使用新的请求 ID")
    if receipt["payload_fingerprint"] != fingerprint:
        raise ChapterCreationError("chapter_creation_payload_conflict", "该请求已提交不同的新章内容，请先核对原创建结果")
    table = models.Chapter.__table__
    current = db.execute(select(table).where(
        table.c.id == receipt["chapter_id"], table.c.novel_id == novel_id,
    )).mappings().first()
    if current is None:
        raise ChapterCreationError("chapter_creation_deleted", "该请求创建的章节已删除，不会重新创建；新章请使用新的请求 ID")
    return {**current, "creation_replayed": True,
            "author_changed_since_creation": any(current[field] != payload[field] for field in AUTHOR_FIELDS)}


def create_manual_chapter(db: Session, novel_id: str, payload: ChapterCreate) -> dict:
    # 拒绝调用方未提交DML必须在try外，不能rollback清掉调用方的其它修改。
    _require_clean_session(db)
    fields = payload.model_dump(mode="json", exclude={"request_id"})
    request_id = str(payload.request_id) if payload.request_id is not None else None
    fingerprint = _fingerprint(novel_id, fields)
    try:
        NovelRepo(db).get(novel_id)
        if request_id:
            previous = _receipt(db, request_id)
            if previous is not None:
                # 成功重放只读，不重新检查可能已删除的大纲，也不修改当前作者稿。
                return _replay(db, novel_id, previous, fingerprint, fields)

        _begin_chapter_write(db)
        NovelRepo(db).get(novel_id)
        if request_id:
            previous = _receipt(db, request_id)
            if previous is not None:
                result = _replay(db, novel_id, previous, fingerprint, fields)
                db.rollback()  # 并发首次创建已成功；释放本次没有DML的写锁。
                return result
        validate_outline_binding(db, novel_id, fields["outline_node_id"])
        chapter = ChapterRepo(db).create(novel_id, commit=False, **fields)
        if request_id:
            db.add(models.ChapterCreationReceipt(
                request_id=request_id, novel_id=novel_id, chapter_id=chapter.id,
                payload_fingerprint=fingerprint,
            ))
            db.flush()
        result = {column.key: getattr(chapter, column.key) for column in models.Chapter.__table__.columns}
        result.update(creation_replayed=False, author_changed_since_creation=False)
        db.commit()
        # 提交后不查库：已保存的正文不能因为响应读取被锁而误报创建失败。
        return result
    except OperationalError:
        db.rollback()
        raise ChapterCreationError("chapter_creation_busy", "创建结果尚未确认，请保留同一请求和内容原样重试") from None
    except Exception:
        db.rollback()
        raise
