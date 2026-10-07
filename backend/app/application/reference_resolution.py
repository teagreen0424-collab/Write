"""把作者确认的引用解析为本作品唯一现存记录；不猜测、不自动新建。"""
from fastapi import HTTPException
from pydantic import ValidationError
from uuid import UUID

from app.infrastructure import models


_NODES = (models.Character, models.Location, models.WorldSetting,
          models.TimelineEvent, models.Foreshadowing)
_FIELDS = {
    "location": {"parent_id": ((models.Location,), False, "上级地点")},
    "timeline_event": {
        "participants": ((models.Character,), True, "参与人物"),
        "cause_id": ((models.TimelineEvent,), False, "因果前序事件"),
    },
    "foreshadowing": {
        "related_characters": ((models.Character,), True, "关联人物"),
        "planted_chapter_id": ((models.Chapter,), False, "埋设章节"),
        "consumed_chapter_id": ((models.Chapter,), False, "消费章节"),
    },
    "relationship": {
        "subject_id": (_NODES, False, "关系主体"),
        "object_id": (_NODES, False, "关系客体"),
    },
}


def _resolve_one(db, novel_id: str, targets, value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(400, detail=f"{label}必须填写有效 ID 或完整名称")
    ids = []
    foreign_id = False
    for model in targets:
        rows = db.query(model).filter(model.id == value).all()
        ids.extend(item.id for item in rows if item.novel_id == novel_id)
        foreign_id = foreign_id or any(item.novel_id != novel_id for item in rows)
    if len(ids) == 1:
        return ids[0]
    if ids:
        raise HTTPException(400, detail=f"{label}的 ID 对应多个记录，请核对")
    if foreign_id:
        raise HTTPException(400, detail=f"{label}的 ID 属于其他作品")
    try:
        UUID(value)
    except ValueError:
        pass
    else:
        raise HTTPException(400, detail=f"{label}的 ID 在当前作品中不存在或引用类型不正确")
    matches = []
    for model in targets:
        field = getattr(model, "name", None)
        if field is None:
            field = getattr(model, "title", None)
        if field is None:
            field = model.content
        matches.extend(db.query(model.id).filter(model.novel_id == novel_id, field == value).all())
    if len(matches) == 1:
        return matches[0].id
    if len(matches) > 1:
        raise HTTPException(400, detail=f"{label}“{value}”有多个同名记录，请填写明确 ID")
    raise HTTPException(400, detail=f"{label}“{value}”在当前作品中不存在，请先创建或修正引用")


def resolve_references(db, novel_id: str, kind: str, data: dict) -> dict:
    if not isinstance(data, dict):
        raise HTTPException(400, detail="变更 data 必须是 JSON 对象")
    resolved = dict(data)
    for field, (targets, multiple, label) in _FIELDS.get(kind, {}).items():
        if field not in resolved or resolved[field] is None:
            continue
        value = resolved[field]
        if multiple:
            if not isinstance(value, list):
                raise HTTPException(400, detail=f"{label}必须是 ID 或名称数组")
            resolved[field] = [_resolve_one(db, novel_id, targets, item, label) for item in value]
        elif value != "":
            resolved[field] = _resolve_one(db, novel_id, targets, value, label)
    return resolved


def failure(index: int, kind: str, error: Exception) -> dict:
    if isinstance(error, HTTPException):
        detail = str(error.detail)
    elif isinstance(error, ValidationError):
        detail = "；".join(f"{'.'.join(map(str, item['loc']))}：{item['msg']}" for item in error.errors())
    elif isinstance(error, ValueError):
        detail = str(error)
    else:
        detail = "此条变更未保存，请核对数据后重试"
    return {"index": index, "type": kind, "error": detail}
