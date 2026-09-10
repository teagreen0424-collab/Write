"""SQLAlchemy ORM 模型：Story Bible 六类实体 + 作品。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Float, ForeignKey, Integer, String, Text
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
    directive: Mapped[str] = mapped_column(Text, default="")


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
