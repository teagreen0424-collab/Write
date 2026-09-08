"""仓储层：封装对 Story Bible 实体的增删改查。"""
from typing import Type

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.domain.entities import ChapterStatus, OutlineLevel
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
        # 显式级联删除作品作用域下的子表数据，避免孤儿残留
        for model in (
            models.Character,
            models.Location,
            models.WorldSetting,
            models.TimelineEvent,
            models.Foreshadowing,
            models.Relationship,
            models.Chapter,
            models.OutlineNode,
        ):
            self.db.query(model).filter(model.novel_id == novel_id).delete()
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
        return (
            self.db.query(self.model)
            .filter(self.model.novel_id == novel_id)
            .order_by(self.model.sort_order)
            .all()
        )

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
        self.db.query(models.Chapter).filter(
            models.Chapter.novel_id == novel_id,
            models.Chapter.outline_node_id == node_id,
        ).update({"outline_node_id": ""})
        self.db.delete(obj)
        self.db.commit()

    def reorder(self, novel_id: str, ordered_ids: list[str]):
        self._ensure_novel(novel_id)
        for idx, node_id in enumerate(ordered_ids):
            obj = self.get(novel_id, node_id)
            obj.sort_order = idx
        self.db.commit()
