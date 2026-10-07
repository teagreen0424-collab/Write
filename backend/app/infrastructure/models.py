"""SQLAlchemy ORM 模型：Story Bible 六类实体 + 作品。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, JSON, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain.entities import ChapterStatus, ForeshadowStatus, OutlineLevel


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[str] = mapped_column(String(40), default=_now)
    updated_at: Mapped[str] = mapped_column(String(40), default=_now, onupdate=_now)


class Novel(Base, TimestampMixin):
    __tablename__ = "novel"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(200))
    genre: Mapped[str] = mapped_column(String(100), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    style_anchor: Mapped[str] = mapped_column(Text, default="")


class Character(Base, TimestampMixin):
    __tablename__ = "character"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(String(36), ForeignKey("novel.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(50), default="")
    personality: Mapped[str] = mapped_column(Text, default="")
    goal: Mapped[str] = mapped_column(Text, default="")
    appearance: Mapped[str] = mapped_column(Text, default="")
    catchphrase: Mapped[str] = mapped_column(Text, default="")
    pov: Mapped[str] = mapped_column(String(50), default="")
    frequency: Mapped[str] = mapped_column(String(100), default="")
    known_info: Mapped[str] = mapped_column(Text, default="")
    extras: Mapped[dict] = mapped_column(JSON, default=dict)


class Location(Base, TimestampMixin):
    __tablename__ = "location"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(String(36), ForeignKey("novel.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    level: Mapped[str] = mapped_column(String(50), default="")
    parent_id: Mapped[str] = mapped_column(String(36), default="")
    description: Mapped[str] = mapped_column(Text, default="")


class WorldSetting(Base, TimestampMixin):
    __tablename__ = "world_setting"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(String(36), ForeignKey("novel.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    category: Mapped[str] = mapped_column(String(50), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)


class TimelineEvent(Base, TimestampMixin):
    __tablename__ = "timeline_event"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(String(36), ForeignKey("novel.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    time_point: Mapped[str] = mapped_column(String(100), default="")
    participants: Mapped[list] = mapped_column(JSON, default=list)
    cause_id: Mapped[str] = mapped_column(String(36), default="")
    description: Mapped[str] = mapped_column(Text, default="")


class Foreshadowing(Base, TimestampMixin):
    __tablename__ = "foreshadowing"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(String(36), ForeignKey("novel.id"), index=True)
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default=ForeshadowStatus.PLANTED.value, index=True)
    related_characters: Mapped[list] = mapped_column(JSON, default=list)
    planted_chapter_id: Mapped[str] = mapped_column(String(36), default="")
    consumed_chapter_id: Mapped[str] = mapped_column(String(36), default="")


class Relationship(Base, TimestampMixin):
    __tablename__ = "relationship"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(String(36), ForeignKey("novel.id"), index=True)
    subject_id: Mapped[str] = mapped_column(String(36))
    object_id: Mapped[str] = mapped_column(String(36))
    relation_type: Mapped[str] = mapped_column(String(50))
    note: Mapped[str] = mapped_column(Text, default="")


class Chapter(Base, TimestampMixin):
    __tablename__ = "chapter"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(String(36), ForeignKey("novel.id"), index=True)
    outline_node_id: Mapped[str] = mapped_column(String(36), default="")
    seq: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(200), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default=ChapterStatus.DRAFT.value)
    summary: Mapped[str] = mapped_column(Text, default="")
    tension_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    drift_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    directive: Mapped[str] = mapped_column(Text, default="")


class ChapterCreationReceipt(Base):
    """手工创建请求的已提交身份；章删除后保留，作品删除时清理。"""

    __tablename__ = "chapter_creation_receipt"

    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    novel_id: Mapped[str] = mapped_column(String(36), ForeignKey("novel.id", ondelete="CASCADE"), index=True)
    # 不外键关联章节：删除后的迟到创建请求不能复活正文。
    chapter_id: Mapped[str] = mapped_column(String(36))
    payload_fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(40), default=_now)


class AutopilotDraft(Base, TimestampMixin):
    """自动驾驶恢复缓存，独立于正文、章节版本和设定事实。

    删除后保留无正文的墓碑，防止浏览器迟到的保存请求复活草稿。
    """

    __tablename__ = "autopilot_draft"
    __table_args__ = (
        CheckConstraint("state IN ('active', 'failed', 'unconfirmed')", name="ck_autopilot_draft_state"),
        CheckConstraint("attempt >= 1", name="ck_autopilot_draft_attempt"),
        CheckConstraint("revision >= 1", name="ck_autopilot_draft_revision"),
        Index("ix_autopilot_draft_scope", "novel_id", "deleted", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(String(36), ForeignKey("novel.id"), index=True)
    node_id: Mapped[str] = mapped_column(String(100), default="")
    title: Mapped[str] = mapped_column(String(200), default="")
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(String(20), default="active")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class AutopilotTask(Base, TimestampMixin):
    """后台任务身份与已提交事件的恢复快照，不属于作品事实。"""

    __tablename__ = "autopilot_task"
    __table_args__ = (
        CheckConstraint(
            "status IN ('starting','running','paused','completed','incomplete','stopped','interrupted')",
            name="ck_autopilot_task_status",
        ),
        Index(
            "uq_autopilot_task_active_novel", "novel_id", unique=True,
            sqlite_where=text("status IN ('starting','running','paused')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    novel_id: Mapped[str] = mapped_column(String(36), ForeignKey("novel.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="starting")
    last_event_id: Mapped[int] = mapped_column(Integer, default=0)
    snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)
    finished_at: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)


class AutopilotTaskEvent(Base):
    """按任务游标持久化的公开事件，订阅者互不影响。"""

    __tablename__ = "autopilot_task_event"
    __table_args__ = (
        UniqueConstraint("task_id", "event_id", name="uq_autopilot_task_event_cursor"),
        Index("ix_autopilot_task_event_scope", "novel_id", "task_id", "event_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("autopilot_task.id"), index=True)
    novel_id: Mapped[str] = mapped_column(String(36), ForeignKey("novel.id"), index=True)
    event_id: Mapped[int] = mapped_column(Integer)
    data_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=_now)


class ChapterRevision(Base):
    """章节历史版本：覆盖既有作者内容前保存的旧状态快照。

    版本记录不可编辑：只在覆盖前插入，恢复时也只读取不修改。
    审阅字段（status/summary/tension_score/drift_score）仅供查看，恢复时只回写作者字段。
    """

    __tablename__ = "chapter_revision"
    __table_args__ = (
        UniqueConstraint("chapter_id", "revision_no", name="uq_chapter_revision_no"),
        Index("ix_chapter_revision_scope", "novel_id", "chapter_id", "revision_no"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novel.id"), index=True
    )
    chapter_id: Mapped[str] = mapped_column(String(36), index=True)
    revision_no: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[str] = mapped_column(String(32), default="manual_save")
    title: Mapped[str] = mapped_column(String(200), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    directive: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default=ChapterStatus.DRAFT.value)
    summary: Mapped[str] = mapped_column(Text, default="")
    tension_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    drift_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(40), default=_now)


class ChapterSettleReceipt(Base):
    """原稿逐项建议的已确认记录；实体删除后保留，防止重试复活设定。"""

    __tablename__ = "chapter_settle_receipt"
    __table_args__ = (
        UniqueConstraint(
            "novel_id", "chapter_id", "source_fingerprint", "suggestion_fingerprint",
            name="uq_chapter_settle_receipt_item",
        ),
        Index("ix_chapter_settle_receipt_scope", "novel_id", "chapter_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(String(36), ForeignKey("novel.id", ondelete="CASCADE"))
    chapter_id: Mapped[str] = mapped_column(String(36), ForeignKey("chapter.id", ondelete="CASCADE"))
    source_fingerprint: Mapped[str] = mapped_column(String(64))
    suggestion_fingerprint: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(30))
    entity_id: Mapped[str] = mapped_column(String(36))
    payload_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=_now)


class OutlineNode(Base, TimestampMixin):
    __tablename__ = "outline_node"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(String(36), ForeignKey("novel.id"), index=True)
    level: Mapped[str] = mapped_column(String(20), default=OutlineLevel.CHAPTER.value)
    title: Mapped[str] = mapped_column(String(200), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    parent_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class MemoryChunk(Base, TimestampMixin):
    __tablename__ = "memory_chunk"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(String(36), ForeignKey("novel.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="chapter_slice")
    source_chapter_id: Mapped[str] = mapped_column(String(36), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    embedding_id: Mapped[str] = mapped_column(String(36), default="")
    vector: Mapped[list] = mapped_column(JSON, default=list)


class Injection(Base):
    __tablename__ = "injection"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(String(36), ForeignKey("novel.id"), index=True)
    channel: Mapped[str] = mapped_column(String(20), default="card")
    raw_text: Mapped[str] = mapped_column(Text, default="")
    parsed_intent: Mapped[dict] = mapped_column(JSON, default=dict)
    applied_changes: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default=_now)


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversation"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("novel.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(200), default="")


class Message(Base):
    __tablename__ = "message"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(String(36), ForeignKey("conversation.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=_now)


class ChatAction(Base):
    """聊天消息的显式落地动作；每条消息同类动作最多完成一次。"""

    __tablename__ = "chat_action"
    __table_args__ = (UniqueConstraint("message_id", "action_type", name="uq_chat_action_message_type"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    message_id: Mapped[str] = mapped_column(String(36), ForeignKey("message.id"), index=True)
    novel_id: Mapped[str] = mapped_column(String(36), ForeignKey("novel.id"), index=True)
    action_type: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="done")
    result_id: Mapped[str] = mapped_column(String(36), default="")
    result_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default=_now)
