"""仓储层：封装对 Story Bible 实体的增删改查。"""
from typing import Type

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.domain.entities import ChapterStatus
from app.infrastructure import models


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
        self.db.commit()
        self.db.refresh(obj)
        return obj

    def update(self, novel_id: str, **data):
        obj = self.get(novel_id)
        for k, v in data.items():
            if v is not None:
                setattr(obj, k, v)
        self.db.commit()
        self.db.refresh(obj)
        return obj

    def delete(self, novel_id: str):
        obj = self.get(novel_id)
        self.db.delete(obj)
        self.db.commit()


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

    def create(self, novel_id: str, **data):
        self._ensure_novel(novel_id)
        if data.get("seq") is None:
            data["seq"] = self.next_seq(novel_id)
        obj = self.model(novel_id=novel_id, **data)
        self.db.add(obj)
        self.db.commit()
        self.db.refresh(obj)
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
        self.db.delete(obj)
        self.db.commit()

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
