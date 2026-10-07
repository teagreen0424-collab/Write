"""设定实体写入与当前向量索引同步；旧索引在同一事务中失效。"""
from math import isfinite
from numbers import Real

from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.infrastructure import models
from app.infrastructure.ai.embedding import EmbeddingService
from app.infrastructure import repository as repos

embedding = EmbeddingService()

_LABELS = {
    "character": "人物", "location": "地点", "world_setting": "世界观",
    "timeline_event": "时间线", "foreshadowing": "伏笔", "relationship": "关系",
}

# 与设定面板、知识图谱的五类节点保持一致，关系本身不能充当端点。
_NODE_MODELS = (
    models.Character, models.Location, models.WorldSetting,
    models.TimelineEvent, models.Foreshadowing,
)
# 限制单次模型输入及SQLite绑定数量；100条关系可在一个向量批次内完成。
_INDEX_BATCH_SIZE = 128
_LOOKUP_BATCH_SIZE = 900


def _require_references(db: Session, novel_id: str, target_models, ids, label: str):
    """引用只允许当前作品的现存记录；错误 ID 类型也返回明确的 400。"""
    if any(not isinstance(value, str) or not value.strip() for value in ids):
        raise HTTPException(status_code=400, detail=f"{label}必须是有效的记录 ID")
    missing = set(ids)
    for model in target_models:
        found = db.query(model.id).filter(model.novel_id == novel_id, model.id.in_(missing)).all()
        missing.difference_update(row.id for row in found)
    if missing:
        raise HTTPException(status_code=400, detail=f"{label}不存在或不属于当前作品")


def _validate_chain(db: Session, obj, field: str, label: str):
    """地点父链与事件因果链不能指回自身或形成环。"""
    visited = {obj.id}
    target_id = getattr(obj, field)
    while target_id:
        if target_id in visited:
            raise HTTPException(status_code=400, detail=f"{label}不能形成循环")
        visited.add(target_id)
        parent = db.query(type(obj)).filter(
            type(obj).novel_id == obj.novel_id, type(obj).id == target_id,
        ).first()
        if parent is None:
            raise HTTPException(status_code=400, detail=f"{label}不存在或不属于当前作品")
        target_id = getattr(parent, field)


def _validate_references(db: Session, obj, changed: dict | None = None):
    """创建校验全部引用，修改仅校验实际提供的字段，兼容旧数据的无关编辑。"""
    def includes(field):
        return changed is None or (field in changed and changed[field] is not None)

    if isinstance(obj, models.Relationship):
        for field in ("subject_id", "object_id"):
            if includes(field):
                _require_references(db, obj.novel_id, _NODE_MODELS, [getattr(obj, field)], "关系端点")
    elif isinstance(obj, models.Location) and includes("parent_id") and obj.parent_id:
        _validate_chain(db, obj, "parent_id", "上级地点")
    elif isinstance(obj, models.TimelineEvent):
        if includes("participants"):
            _require_references(db, obj.novel_id, (models.Character,), obj.participants, "参与人物")
        if includes("cause_id") and obj.cause_id:
            _validate_chain(db, obj, "cause_id", "因果前序事件")
    elif isinstance(obj, models.Foreshadowing):
        if includes("related_characters"):
            _require_references(db, obj.novel_id, (models.Character,), obj.related_characters, "关联人物")
        for field, label in (("planted_chapter_id", "埋设章节"), ("consumed_chapter_id", "消费章节")):
            if includes(field) and getattr(obj, field):
                _require_references(db, obj.novel_id, (models.Chapter,), [getattr(obj, field)], label)


def _display_name(obj) -> str:
    return (getattr(obj, "name", None) or getattr(obj, "title", None)
            or getattr(obj, "content", ""))


def _endpoint_names(db: Session, novel_id: str, relationships: list) -> dict:
    """同一作品的关系端点一次预取，不按每条关系重复查五类实体。"""
    endpoint_ids = list({value for relation in relationships
                         for value in (relation.subject_id, relation.object_id) if value})
    names = {}
    for model in _NODE_MODELS:
        label = next(getattr(model, field) for field in ("name", "title", "content") if hasattr(model, field))
        for start in range(0, len(endpoint_ids), _LOOKUP_BATCH_SIZE):
            rows = db.query(model.id, label).filter(
                model.novel_id == novel_id,
                model.id.in_(endpoint_ids[start:start + _LOOKUP_BATCH_SIZE]),
            ).all()
            names.update({row[0]: row[1] or "" for row in rows})
    return names


def _source_text(db: Session, obj, names: dict | None = None) -> str:
    """从已刷新实体构造索引文本；排除主键、作品 ID 与时间戳。"""
    if names is None:
        names = _endpoint_names(db, obj.novel_id, [obj]) if isinstance(obj, models.Relationship) else {}
    fields = []
    for column in obj.__table__.columns:
        if column.name in {"id", "novel_id", "created_at", "updated_at"}:
            continue
        value = getattr(obj, column.name)
        if value in (None, "", [], {}):
            continue
        if column.name in {"subject_id", "object_id"}:
            value = names.get(value, "人物已删除")
        fields.append(f"{column.name}：{value}")
    return _LABELS.get(obj.__tablename__, obj.__tablename__) + "；" + "；".join(fields)


def _valid_vector(value) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or not value:
        return None
    if any(not isinstance(number, Real) or isinstance(number, bool) or not isfinite(number) for number in value):
        return None
    return [float(number) for number in value]


def _replace_indices(db: Session, novel_id: str, objects: list) -> list[bool]:
    """批量替换仍由调用方统一提交；嵌入失败也失效旧块，不猜错位对应。"""
    if not objects:
        return []
    names = _endpoint_names(db, novel_id, [obj for obj in objects if isinstance(obj, models.Relationship)])
    texts = [_source_text(db, obj, names) for obj in objects]
    vectors = [None] * len(objects)
    for start in range(0, len(texts), _INDEX_BATCH_SIZE):
        batch = texts[start:start + _INDEX_BATCH_SIZE]
        try:
            result = embedding.embed(batch)
            if not isinstance(result, (list, tuple)) or len(result) != len(batch):
                raise ValueError("嵌入数量与输入不一致，无法可靠对应")
            valid = [_valid_vector(value) for value in result]
        except Exception:  # noqa: BLE001  模型故障只令本批索引缺失，作者变更仍可保存
            continue
        vectors[start:start + len(batch)] = valid

    target_ids = [obj.id for obj in objects]
    for start in range(0, len(target_ids), _LOOKUP_BATCH_SIZE):
        db.query(models.MemoryChunk).filter(
            models.MemoryChunk.novel_id == novel_id,
            models.MemoryChunk.kind == "setting",
            models.MemoryChunk.embedding_id.in_(target_ids[start:start + _LOOKUP_BATCH_SIZE]),
        ).delete(synchronize_session=False)
    db.add_all([models.MemoryChunk(
        novel_id=novel_id, kind="setting", source_chapter_id="",
        embedding_id=obj.id, text=text, vector=vector,
    ) for obj, text, vector in zip(objects, texts, vectors) if vector is not None])
    return [vector is not None for vector in vectors]


def _replace_index(db: Session, novel_id: str, obj) -> bool:
    return _replace_indices(db, novel_id, [obj])[0]


def _commit_setting(db: Session, obj):
    """提交前冻结全列，保留ORM身份且不以提交后的读失败否认已保存事实。"""
    db.flush()
    frozen = {column.key: getattr(obj, column.key) for column in obj.__table__.columns}
    db.commit()
    for key, value in frozen.items():
        set_committed_value(obj, key, value)


def create_setting(db: Session, repo_class, novel_id: str, *, commit: bool = True, **data):
    repo = repo_class(db)
    repo._ensure_novel(novel_id)
    try:
        obj = repo.model(novel_id=novel_id, **data)
        db.add(obj)
        db.flush()
        _validate_references(db, obj)
        _replace_index(db, novel_id, obj)
        if commit:
            _commit_setting(db, obj)
        return obj
    except Exception:
        db.rollback()
        raise


def update_setting(
    db: Session, repo_class, novel_id: str, item_id: str, *, commit: bool = True, **data,
):
    repo = repo_class(db)
    obj = repo.get(novel_id, item_id)
    old_display_name = _display_name(obj) if isinstance(obj, _NODE_MODELS) else None
    try:
        for key, value in data.items():
            if value is not None:
                setattr(obj, key, value)
        db.flush()
        _validate_references(db, obj, data)
        _replace_index(db, novel_id, obj)
        if isinstance(obj, _NODE_MODELS) and _display_name(obj) != old_display_name:
            _refresh_entity_relationships(db, novel_id, obj.id)
        if commit:
            _commit_setting(db, obj)
        return obj
    except Exception:
        db.rollback()
        raise


def delete_setting(db: Session, repo_class, novel_id: str, item_id: str):
    obj = repo_class(db).get(novel_id, item_id)
    try:
        db.query(models.MemoryChunk).filter(
            models.MemoryChunk.novel_id == novel_id,
            models.MemoryChunk.kind == "setting",
            models.MemoryChunk.embedding_id == item_id,
        ).delete(synchronize_session=False)
        affected = _unlink_deleted_setting(db, novel_id, obj)
        db.delete(obj)
        db.flush()
        _replace_indices(db, novel_id, affected)
        if isinstance(obj, _NODE_MODELS):
            _refresh_entity_relationships(db, novel_id, item_id)
        db.commit()
    except Exception:
        db.rollback()
        raise


def _unlink_deleted_setting(db: Session, novel_id: str, obj) -> list:
    """只解除当前作品的引用；保留已有关系记录及其“人物已删除”提示。"""
    affected = []
    if isinstance(obj, models.Character):
        for model, field in ((models.TimelineEvent, "participants"),
                             (models.Foreshadowing, "related_characters")):
            for item in db.query(model).filter(model.novel_id == novel_id).all():
                values = getattr(item, field)
                if obj.id in values:
                    setattr(item, field, [value for value in values if value != obj.id])
                    affected.append(item)
    elif isinstance(obj, (models.Location, models.TimelineEvent)):
        field = "parent_id" if isinstance(obj, models.Location) else "cause_id"
        affected = db.query(type(obj)).filter(
            type(obj).novel_id == novel_id, getattr(type(obj), field) == obj.id,
        ).all()
        for item in affected:
            setattr(item, field, "")
    return affected


def _refresh_entity_relationships(db: Session, novel_id: str, entity_id: str):
    relationships = db.query(models.Relationship).filter(
        models.Relationship.novel_id == novel_id,
        (models.Relationship.subject_id == entity_id)
        | (models.Relationship.object_id == entity_id),
    ).all()
    _replace_indices(db, novel_id, relationships)


def rebuild_settings_index(db: Session, novel_id: str) -> dict:
    """显式重建本作品全部设定索引；不触碰章节切片。"""
    repos.NovelRepo(db).get(novel_id)
    classes = (
        repos.CharacterRepo, repos.LocationRepo, repos.WorldSettingRepo,
        repos.TimelineEventRepo, repos.ForeshadowingRepo, repos.RelationshipRepo,
    )
    try:
        db.query(models.MemoryChunk).filter(
            models.MemoryChunk.novel_id == novel_id,
            models.MemoryChunk.kind == "setting",
        ).delete(synchronize_session=False)
        objects = []
        for repo_class in classes:
            objects.extend(repo_class(db).list(novel_id))
        indexed = sum(_replace_indices(db, novel_id, objects))
        db.commit()
        return {"indexed": indexed, "failed": len(objects) - indexed}
    except Exception:
        db.rollback()
        raise
