"""仓储层：封装对 Story Bible 实体的增删改查。"""
from typing import Optional, Type

from fastapi import HTTPException
from sqlalchemy import and_, func, literal_column, or_
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.domain.entities import ChapterStatus, OutlineLevel
from app.infrastructure import models


def _chapter_word_count(content: str | None) -> int:
    """SQLite回调：与正文/版本非空白Unicode字符计数一致，保留emoji、NUL及零宽字符。"""
    return len("".join(content.split())) if content else 0


def _chapter_is_written(content: str | None) -> bool:
    """伏笔已写口径保留正文strip语义；无需为布尔结果拼接整段正文。"""
    return bool((content or "").strip())


class ConversationReadError(HTTPException):
    """聊天只读查询的可判定作用域/游标错误。"""

    def __init__(self, status_code: int, detail: str, code: str):
        super().__init__(status_code=status_code, detail=detail)
        self.code = code


class NovelRepo:
    """作品仓储。"""

    model: Type = models.Novel

    def __init__(self, db: Session):
        self.db = db

    def list(self):
        return self.db.query(self.model).order_by(self.model.created_at.desc()).all()

    def get(self, novel_id: str):
        obj = self.db.query(self.model).filter(self.model.id == novel_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="作品不存在")
        return obj

    def create(self, **data):
        obj = self.model(**data)
        self.db.add(obj)
        self.db.flush()
        self.db.refresh(obj)
        frozen = {column.key: getattr(obj, column.key) for column in obj.__table__.columns}
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        for field, value in frozen.items():
            set_committed_value(obj, field, value)
        return obj

    def update(self, novel_id: str, **data):
        obj = self.get(novel_id)
        for k, v in data.items():
            if v is not None:
                setattr(obj, k, v)
        self.db.flush()
        self.db.refresh(obj)
        frozen = {column.key: getattr(obj, column.key) for column in obj.__table__.columns}
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        for field, value in frozen.items():
            set_committed_value(obj, field, value)
        return obj

    def delete(self, novel_id: str):
        obj = self.get(novel_id)
        try:
            self.db.query(models.ChatAction).filter(
                models.ChatAction.novel_id == novel_id
            ).delete(synchronize_session=False)
            # 先删作品下的会话及其消息（message 无 novel_id，需按 conversation_id 删）
            conv_ids = [
                c.id
                for c in self.db.query(models.Conversation)
                .filter(models.Conversation.novel_id == novel_id)
                .all()
            ]
            if conv_ids:
                message_ids = [
                    m.id for m in self.db.query(models.Message.id).filter(
                        models.Message.conversation_id.in_(conv_ids)
                    ).all()
                ]
                if message_ids:
                    self.db.query(models.ChatAction).filter(
                        models.ChatAction.message_id.in_(message_ids)
                    ).delete(synchronize_session=False)
                self.db.query(models.Message).filter(
                    models.Message.conversation_id.in_(conv_ids)
                ).delete(synchronize_session=False)
            self.db.query(models.Conversation).filter(
                models.Conversation.novel_id == novel_id
            ).delete(synchronize_session=False)
            # 显式级联删除作品作用域下的子表数据，避免孤儿残留
            for model in (
                models.AutopilotTaskEvent,
                models.AutopilotTask,
                models.ChapterSettleReceipt,
                models.ChapterCreationReceipt,
                models.Character,
                models.Location,
                models.WorldSetting,
                models.TimelineEvent,
                models.Foreshadowing,
                models.Relationship,
                models.Chapter,
                models.ChapterRevision,
                models.AutopilotDraft,
                models.OutlineNode,
                models.MemoryChunk,
                models.Injection,
            ):
                self.db.query(model).filter(model.novel_id == novel_id).delete()
            self.db.delete(obj)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise


class ScopedRepo:
    """作品作用域下的实体仓储基类（六类设定实体共用）。"""

    model: Type = None

    def __init__(self, db: Session):
        self.db = db

    def _ensure_novel(self, novel_id: str) -> None:
        exists = self.db.query(models.Novel).filter(models.Novel.id == novel_id).first()
        if not exists:
            raise HTTPException(status_code=404, detail="作品不存在")

    def list(self, novel_id: str):
        self._ensure_novel(novel_id)
        return (
            self.db.query(self.model)
            .filter(self.model.novel_id == novel_id)
            .order_by(self.model.created_at)
            .all()
        )

    def get(self, novel_id: str, item_id: str):
        obj = (
            self.db.query(self.model)
            .filter(self.model.novel_id == novel_id, self.model.id == item_id)
            .first()
        )
        if not obj:
            raise HTTPException(status_code=404, detail="记录不存在")
        return obj

    def create(self, novel_id: str, **data):
        self._ensure_novel(novel_id)
        obj = self.model(novel_id=novel_id, **data)
        self.db.add(obj)
        self.db.commit()
        self.db.refresh(obj)
        return obj

    def update(self, novel_id: str, item_id: str, **data):
        obj = self.get(novel_id, item_id)
        for k, v in data.items():
            if v is not None:
                setattr(obj, k, v)
        self.db.commit()
        self.db.refresh(obj)
        return obj

    def delete(self, novel_id: str, item_id: str):
        obj = self.get(novel_id, item_id)
        self.db.delete(obj)
        self.db.commit()


class CharacterRepo(ScopedRepo):
    model = models.Character


class LocationRepo(ScopedRepo):
    model = models.Location


class WorldSettingRepo(ScopedRepo):
    model = models.WorldSetting


class TimelineEventRepo(ScopedRepo):
    model = models.TimelineEvent


class ForeshadowingRepo(ScopedRepo):
    model = models.Foreshadowing


class RelationshipRepo(ScopedRepo):
    model = models.Relationship


class ChapterRepo:
    """章节仓储。"""

    model = models.Chapter

    def __init__(self, db: Session):
        self.db = db

    def _ensure_novel(self, novel_id: str) -> None:
        exists = self.db.query(models.Novel).filter(models.Novel.id == novel_id).first()
        if not exists:
            raise HTTPException(status_code=404, detail="作品不存在")

    def list(self, novel_id: str):
        self._ensure_novel(novel_id)
        return (
            self.db.query(self.model)
            .filter(self.model.novel_id == novel_id)
            .order_by(self.model.seq)
            .all()
        )

    def list_metadata(self, novel_id: str):
        """目录只投影轻量列；正文逐行交给确定性计数函数，不加载整章ORM。"""
        self._ensure_novel(novel_id)
        # 注册当前实际连接，兼容已建立的池连接及隔离测试；不提交或回滚调用方事务。
        self.db.connection().connection.driver_connection.create_function(
            "lingmo_chapter_word_count", 1, _chapter_word_count, deterministic=True,
        )
        return self.db.query(
            self.model.id, self.model.novel_id, self.model.seq, self.model.title,
            self.model.status, self.model.outline_node_id, self.model.created_at,
            self.model.updated_at, self.model.tension_score, self.model.drift_score,
            func.lingmo_chapter_word_count(self.model.content).label("word_count"),
        ).filter(self.model.novel_id == novel_id).order_by(self.model.seq).all()

    def list_quality_curve(self, novel_id: str):
        """质量曲线只读五个展示字段，绕过已缓存章节，保留调用方autoflush约定。"""
        self._ensure_novel(novel_id)
        return self.db.query(
            self.model.seq, self.model.title, self.model.status,
            self.model.tension_score, self.model.drift_score,
        ).filter(self.model.novel_id == novel_id).order_by(self.model.seq).all()

    def list_tracking_chapters(self, novel_id: str):
        """伏笔聚合只取ID、序号与已写标记，正文不作为查询结果返回。"""
        self._ensure_novel(novel_id)
        self.db.connection().connection.driver_connection.create_function(
            "lingmo_chapter_is_written", 1, _chapter_is_written, deterministic=True,
        )
        return self.db.query(
            self.model.id, self.model.seq,
            func.lingmo_chapter_is_written(self.model.content).label("is_written"),
        ).filter(self.model.novel_id == novel_id).order_by(self.model.seq).all()

    def list_book_metadata(self, novel_id: str):
        """交付统计只取八个目录字段，复用准确Unicode字数回调。"""
        self._ensure_novel(novel_id)
        self.db.connection().connection.driver_connection.create_function(
            "lingmo_chapter_word_count", 1, _chapter_word_count, deterministic=True,
        )
        return self.db.query(
            self.model.id, self.model.seq, self.model.title, self.model.status,
            self.model.outline_node_id, self.model.tension_score, self.model.drift_score,
            func.lingmo_chapter_word_count(self.model.content).label("word_count"),
        ).filter(self.model.novel_id == novel_id).order_by(self.model.seq).all()

    def get(self, novel_id: str, chapter_id: str):
        obj = (
            self.db.query(self.model)
            .filter(self.model.novel_id == novel_id, self.model.id == chapter_id)
            .first()
        )
        if not obj:
            raise HTTPException(status_code=404, detail="章节不存在")
        return obj

    def next_seq(self, novel_id: str) -> int:
        max_seq = (
            self.db.query(func.max(self.model.seq))
            .filter(self.model.novel_id == novel_id)
            .scalar()
        )
        return (max_seq or 0) + 1

    def create(self, novel_id: str, *, commit: bool = True, **data):
        self._ensure_novel(novel_id)
        if data.get("seq") is None:
            data["seq"] = self.next_seq(novel_id)
        obj = self.model(novel_id=novel_id, **data)
        self.db.add(obj)
        self.db.flush()
        if not commit:
            return obj
        # 调用方可能已平移章节序号；保留同一事务，提交前冻结完整响应。
        frozen = {column.key: getattr(obj, column.key) for column in self.model.__table__.columns}
        self.db.commit()
        for key, value in frozen.items():
            set_committed_value(obj, key, value)
        return obj

    def update(self, novel_id: str, chapter_id: str, **data):
        obj = self.get(novel_id, chapter_id)
        for k, v in data.items():
            if v is not None:
                setattr(obj, k, v)
        self.db.commit()
        self.db.refresh(obj)
        return obj

    def delete(self, novel_id: str, chapter_id: str):
        obj = self.get(novel_id, chapter_id)
        try:
            self.db.query(models.ChapterSettleReceipt).filter(
                models.ChapterSettleReceipt.novel_id == novel_id,
                models.ChapterSettleReceipt.chapter_id == chapter_id,
            ).delete(synchronize_session=False)
            # 删除章节时同步清理它派生出的记忆，避免后续检索召回已经删除的正文。
            self.db.query(models.MemoryChunk).filter(
                models.MemoryChunk.novel_id == novel_id,
                models.MemoryChunk.source_chapter_id == chapter_id,
            ).delete(synchronize_session=False)
            # 章节删除仍是永久删除：历史版本一并清理，不保留回收站。
            self.db.query(models.ChapterRevision).filter(
                models.ChapterRevision.novel_id == novel_id,
                models.ChapterRevision.chapter_id == chapter_id,
            ).delete(synchronize_session=False)
            # 伏笔本身属于 Story Bible，只解除对已删除章节的引用，不删除伏笔记录。
            planted = (
                self.db.query(models.Foreshadowing)
                .filter(
                    models.Foreshadowing.novel_id == novel_id,
                    models.Foreshadowing.planted_chapter_id == chapter_id,
                )
                .all()
            )
            consumed = (
                self.db.query(models.Foreshadowing)
                .filter(
                    models.Foreshadowing.novel_id == novel_id,
                    models.Foreshadowing.consumed_chapter_id == chapter_id,
                )
                .all()
            )
            for item in planted:
                item.planted_chapter_id = ""
            for item in consumed:
                item.consumed_chapter_id = ""
            # 引用变化与章节删除同一事务失效旧设定索引，避免召回已删除章节 ID。
            affected_ids = {item.id for item in planted + consumed}
            if affected_ids:
                self.db.query(models.MemoryChunk).filter(
                    models.MemoryChunk.novel_id == novel_id,
                    models.MemoryChunk.kind == "setting",
                    models.MemoryChunk.embedding_id.in_(affected_ids),
                ).delete(synchronize_session=False)
            self.db.delete(obj)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def finalized(self, novel_id: str):
        """已定稿章节（按 seq 排序），用于导出与摘要链。"""
        return (
            self.db.query(self.model)
            .filter(
                self.model.novel_id == novel_id,
                self.model.status == ChapterStatus.FINALIZED.value,
            )
            .order_by(self.model.seq)
            .all()
        )

    def recent_finalized_summaries(self, novel_id: str):
        """聊天只取最后三章的序号/摘要；SQLite并列seq保留原行顺序。"""
        rows = (
            self.db.query(self.model.seq, self.model.summary)
            .filter(
                self.model.novel_id == novel_id,
                self.model.status == ChapterStatus.FINALIZED.value,
            )
            .order_by(self.model.seq.desc(), literal_column("chapter.rowid").desc())
            .limit(3)
            .all()
        )
        return list(reversed(rows))


class ChapterRevisionRepo:
    """章节历史版本仓储。

    与其它仓储不同，本仓储的写方法只把对象加入当前会话，**不自行 commit**，
    由 application/revisions.py 保证「快照 + 章节更新」落在同一次事务里。
    """

    model = models.ChapterRevision

    def __init__(self, db: Session):
        self.db = db

    def list_by_chapter(self, novel_id: str, chapter_id: str):
        """按版本号倒序列出某章全部版本。"""
        return (
            self.db.query(self.model)
            .filter(self.model.novel_id == novel_id, self.model.chapter_id == chapter_id)
            .order_by(self.model.revision_no.desc())
            .all()
        )

    def get(self, novel_id: str, chapter_id: str, revision_id: str):
        """在作品 + 章节双重作用域下读取单个版本，跨作用域一律 404。"""
        obj = (
            self.db.query(self.model)
            .filter(
                self.model.id == revision_id,
                self.model.novel_id == novel_id,
                self.model.chapter_id == chapter_id,
            )
            .first()
        )
        if not obj:
            raise HTTPException(status_code=404, detail="章节版本不存在")
        return obj

    def next_revision_no(self, chapter_id: str) -> int:
        """该章下一个可用版本号（从 1 开始）。"""
        max_no = (
            self.db.query(func.max(self.model.revision_no))
            .filter(self.model.chapter_id == chapter_id)
            .scalar()
        )
        return (max_no or 0) + 1

    def add_snapshot(self, chapter, source: str, word_count: int):
        """把章节当前状态作为快照加入当前事务，不提交。"""
        obj = self.model(
            novel_id=chapter.novel_id,
            chapter_id=chapter.id,
            revision_no=self.next_revision_no(chapter.id),
            source=source,
            title=chapter.title or "",
            content=chapter.content or "",
            directive=chapter.directive or "",
            status=chapter.status or "",
            summary=chapter.summary or "",
            tension_score=chapter.tension_score,
            drift_score=chapter.drift_score,
            word_count=word_count,
        )
        self.db.add(obj)
        return obj

    def delete_by_chapter(self, novel_id: str, chapter_id: str) -> int:
        """按章节清理版本（恢复正文前让旧记忆失效时也走这里）。"""
        return (
            self.db.query(self.model)
            .filter(self.model.novel_id == novel_id, self.model.chapter_id == chapter_id)
            .delete(synchronize_session=False)
        )

    def delete_by_novel(self, novel_id: str) -> int:
        """按作品清理版本。"""
        return (
            self.db.query(self.model)
            .filter(self.model.novel_id == novel_id)
            .delete(synchronize_session=False)
        )


class OutlineRepo:
    """大纲节点仓储（卷/幕/章三级）。"""

    model = models.OutlineNode

    def __init__(self, db: Session):
        self.db = db

    def _ensure_novel(self, novel_id: str) -> None:
        exists = self.db.query(models.Novel).filter(models.Novel.id == novel_id).first()
        if not exists:
            raise HTTPException(status_code=404, detail="作品不存在")

    def list_all(self, novel_id: str):
        self._ensure_novel(novel_id)
        nodes = (
            self.db.query(self.model)
            .filter(self.model.novel_id == novel_id)
            .all()
        )
        children: dict[str, list] = {}
        for node in nodes:
            children.setdefault(node.parent_id, []).append(node)
        for siblings in children.values():
            siblings.sort(key=lambda node: (node.sort_order, node.created_at, node.id))

        ordered = []
        visited: set[str] = set()

        def visit(node):
            if node.id in visited:
                return
            visited.add(node.id)
            ordered.append(node)
            for child in children.get(node.id, []):
                visit(child)

        for root in children.get("", []):
            visit(root)
        # 兼容旧数据中的孤立节点，同时防止异常父链导致节点从列表消失。
        for node in sorted(nodes, key=lambda item: (item.sort_order, item.created_at, item.id)):
            visit(node)
        return ordered

    def children(self, novel_id: str, parent_id: str = ""):
        self._ensure_novel(novel_id)
        return (
            self.db.query(self.model)
            .filter(self.model.novel_id == novel_id, self.model.parent_id == parent_id)
            .order_by(self.model.sort_order)
            .all()
        )

    def get(self, novel_id: str, node_id: str):
        obj = (
            self.db.query(self.model)
            .filter(self.model.novel_id == novel_id, self.model.id == node_id)
            .first()
        )
        if not obj:
            raise HTTPException(status_code=404, detail="大纲节点不存在")
        return obj

    def create(self, novel_id: str, **data):
        self._ensure_novel(novel_id)
        if data.get("sort_order") is None:
            max_order = (
                self.db.query(func.max(self.model.sort_order))
                .filter(
                    self.model.novel_id == novel_id,
                    self.model.parent_id == data.get("parent_id", ""),
                )
                .scalar()
            )
            data["sort_order"] = (max_order or 0) + 1
        obj = self.model(novel_id=novel_id, **data)
        self.db.add(obj)
        self.db.commit()
        self.db.refresh(obj)
        return obj

    def update(self, novel_id: str, node_id: str, **data):
        obj = self.get(novel_id, node_id)
        for k, v in data.items():
            if v is not None:
                setattr(obj, k, v)
        self.db.commit()
        self.db.refresh(obj)
        return obj

    def delete(self, novel_id: str, node_id: str):
        obj = self.get(novel_id, node_id)
        if self.children(novel_id, node_id):
            raise HTTPException(status_code=400, detail="该节点下还有子节点，请先删除子节点")
        # 删除节点不级联删正文：将关联章节的 outline_node_id 置空
        try:
            self.db.query(models.Chapter).filter(
                models.Chapter.novel_id == novel_id,
                models.Chapter.outline_node_id == node_id,
            ).update({"outline_node_id": ""})
            self.db.delete(obj)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def reorder(self, novel_id: str, ordered_ids: list[str]):
        self._ensure_novel(novel_id)
        for idx, node_id in enumerate(ordered_ids):
            obj = self.get(novel_id, node_id)
            obj.sort_order = idx
        self.db.commit()


class MemoryChunkRepo:
    """记忆/知识块仓储（正文切片与三元组）。"""

    model = models.MemoryChunk

    def __init__(self, db: Session):
        self.db = db

    def create(self, novel_id: str, **data):
        obj = self.model(novel_id=novel_id, **data)
        self.db.add(obj)
        self.db.commit()
        self.db.refresh(obj)
        return obj

    def list_by_novel(self, novel_id: str):
        return self.db.query(self.model).filter(self.model.novel_id == novel_id).all()


class InjectionRepo:
    """构思注入记录仓储。"""

    model = models.Injection

    def __init__(self, db: Session):
        self.db = db

    def create(self, novel_id: str, **data):
        obj = self.model(novel_id=novel_id, **data)
        self.db.add(obj)
        self.db.commit()
        self.db.refresh(obj)
        return obj

    def list_by_novel(self, novel_id: str):
        return self.db.query(self.model).filter(self.model.novel_id == novel_id).all()


class ConversationRepo:
    """会话仓储（全局 + 作品会话），含消息读写。"""

    model = models.Conversation

    def __init__(self, db: Session):
        self.db = db

    def _ensure_novel(self, novel_id: str) -> None:
        exists = self.db.query(models.Novel).filter(models.Novel.id == novel_id).first()
        if not exists:
            raise HTTPException(status_code=404, detail="作品不存在")

    def list(self):
        return self.db.query(self.model).order_by(self.model.updated_at.desc()).all()

    def get(self, conversation_id: str):
        obj = self.db.query(self.model).filter(self.model.id == conversation_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail="会话不存在")
        return obj

    def get_scope(self, conversation_id: str):
        """只读取真实会话归属列，保持调用方默认autoflush与显式no_autoflush。"""
        scope = self.db.query(self.model.id, self.model.novel_id).filter(
            self.model.id == conversation_id,
        ).first()
        if scope is None:
            raise ConversationReadError(404, "会话不存在", "chat_conversation_not_found")
        return scope

    def create(self, novel_id: Optional[str] = None, title: str = ""):
        if novel_id is not None:
            self._ensure_novel(novel_id)
        obj = self.model(novel_id=novel_id, title=title)
        self.db.add(obj)
        self.db.flush()
        self.db.refresh(obj)
        frozen = {column.key: getattr(obj, column.key) for column in self.model.__table__.columns}
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        for key, value in frozen.items():
            set_committed_value(obj, key, value)
        return obj

    def update(self, conversation_id: str, **data):
        obj = self.get(conversation_id)
        if data.get("novel_id") is not None:
            if obj.novel_id and data["novel_id"] != obj.novel_id:
                raise HTTPException(status_code=400, detail="已绑定作品的会话不能移动到其他作品")
            self._ensure_novel(data["novel_id"])
        for k, v in data.items():
            if v is not None:
                setattr(obj, k, v)
        self.db.flush()
        self.db.refresh(obj)
        frozen = {column.key: getattr(obj, column.key) for column in self.model.__table__.columns}
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        for key, value in frozen.items():
            set_committed_value(obj, key, value)
        return obj

    def delete(self, conversation_id: str):
        obj = self.get(conversation_id)
        try:
            message_ids = [
                m.id for m in self.db.query(models.Message.id).filter(
                    models.Message.conversation_id == conversation_id
                ).all()
            ]
            if message_ids:
                self.db.query(models.ChatAction).filter(
                    models.ChatAction.message_id.in_(message_ids)
                ).delete(synchronize_session=False)
            self.db.query(models.Message).filter(
                models.Message.conversation_id == conversation_id
            ).delete(synchronize_session=False)
            self.db.delete(obj)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def add_message(
        self, conversation_id: str, role: str, content: str, meta=None,
        *, message_id: str | None = None, commit: bool = True,
    ):
        """共享写入保持原调用方提交语义；提交后返回同一身份，不再refresh。"""
        conv = self.get(conversation_id)
        obj = models.Message(
            conversation_id=conversation_id, role=role, content=content, meta=meta
        )
        if message_id is not None:
            obj.id = message_id
        self.db.add(obj)
        conv.updated_at = models._now()
        self.db.flush()
        if not commit:
            return obj
        frozen = {column.key: getattr(obj, column.key) for column in models.Message.__table__.columns}
        self.db.commit()
        for key, value in frozen.items():
            set_committed_value(obj, key, value)
        return obj

    def list_messages(self, conversation_id: str):
        self.get(conversation_id)
        return (
            self.db.query(models.Message)
            .filter(models.Message.conversation_id == conversation_id)
            .order_by(models.Message.created_at)
            .all()
        )

    def list_message_window(self, conversation_id: str, limit: int = 50, before_message_id: str | None = None):
        """完整消息有界窗口；游标只接受同会话真实消息，不暴露/相信客户端rowid。"""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ConversationReadError(422, "每页消息数必须为1至100的整数", "chat_message_window_invalid")
        if before_message_id is not None and (not isinstance(before_message_id, str) or not 1 <= len(before_message_id) <= 200):
            raise ConversationReadError(422, "消息游标格式无效", "chat_message_window_invalid")
        scope = self.get_scope(conversation_id)
        message = models.Message
        rowid = literal_column("message.rowid")
        query = self.db.query(
            message.id, message.conversation_id, message.role, message.content, message.meta, message.created_at,
        ).filter(message.conversation_id == conversation_id)
        if before_message_id is not None:
            anchor = self.db.query(message.created_at, rowid.label("rowid")).filter(
                message.id == before_message_id, message.conversation_id == conversation_id,
            ).first()
            if anchor is None:
                raise ConversationReadError(404, "消息游标不属于当前会话或已不存在", "chat_message_cursor_not_found")
            query = query.filter(or_(message.created_at < anchor.created_at,
                and_(message.created_at == anchor.created_at, rowid < anchor.rowid)))
        rows = query.order_by(message.created_at.desc(), rowid.desc()).limit(limit + 1).all()
        has_more = len(rows) > limit
        items = list(reversed(rows[:limit]))
        return {"conversation_id": scope.id, "novel_id": scope.novel_id, "items": items,
                "has_more": has_more, "next_before_message_id": items[0].id if has_more else None}

    def recent_messages(self, conversation_id: str):
        """模型只读最近二十条role/content；并列时间戳按SQLite原行顺序截取。"""
        self.get(conversation_id)
        rows = (
            self.db.query(models.Message.role, models.Message.content)
            .filter(models.Message.conversation_id == conversation_id)
            .order_by(models.Message.created_at.desc(), literal_column("message.rowid").desc())
            .limit(20)
            .all()
        )
        return list(reversed(rows))

    def update_message(self, conversation_id: str, message_id: str, meta=None):
        obj = (
            self.db.query(models.Message)
            .filter(
                models.Message.conversation_id == conversation_id,
                models.Message.id == message_id,
            )
            .first()
        )
        if not obj:
            raise HTTPException(status_code=404, detail="消息不存在")
        obj.meta = {**(obj.meta or {}), **(meta or {})}
        self.db.commit()
        self.db.refresh(obj)
        return obj
