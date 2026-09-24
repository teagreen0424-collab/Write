"""设定实体写入与当前向量索引同步；旧索引在同一事务中失效。"""
from sqlalchemy.orm import Session

from app.infrastructure import models
from app.infrastructure.ai.embedding import EmbeddingService
from app.infrastructure import repository as repos

embedding = EmbeddingService()

_LABELS = {
    "character": "人物", "location": "地点", "world_setting": "世界观",
    "timeline_event": "时间线", "foreshadowing": "伏笔", "relationship": "关系",
}


def _source_text(db: Session, obj) -> str:
    """从已刷新实体构造索引文本；排除主键、作品 ID 与时间戳。"""
    names = {}
    if isinstance(obj, models.Relationship):
        people = db.query(models.Character).filter(
            models.Character.novel_id == obj.novel_id,
            models.Character.id.in_([obj.subject_id, obj.object_id]),
        ).all()
        names = {person.id: person.name for person in people}
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


def _replace_index(db: Session, novel_id: str, obj) -> bool:
    """先计算新向量，失败时仍删除旧块；调用方统一提交。"""
    text = _source_text(db, obj)
    try:
        vector = embedding.embed([text])[0]
        if not vector:
            raise ValueError("嵌入结果为空")
    except Exception:  # noqa: BLE001  设定写入不因索引故障失败，但旧块必须失效
        vector = None
    db.query(models.MemoryChunk).filter(
        models.MemoryChunk.novel_id == novel_id,
        models.MemoryChunk.kind == "setting",
        models.MemoryChunk.embedding_id == obj.id,
    ).delete(synchronize_session=False)
    if vector is not None:
        db.add(models.MemoryChunk(
            novel_id=novel_id, kind="setting", source_chapter_id="",
            embedding_id=obj.id, text=text, vector=vector,
        ))
    return vector is not None


def create_setting(db: Session, repo_class, novel_id: str, *, commit: bool = True, **data):
    repo = repo_class(db)
    repo._ensure_novel(novel_id)
    try:
        obj = repo.model(novel_id=novel_id, **data)
        db.add(obj)
        db.flush()
        _replace_index(db, novel_id, obj)
        if commit:
            db.commit()
            db.refresh(obj)
        return obj
    except Exception:
        db.rollback()
        raise


def update_setting(
    db: Session, repo_class, novel_id: str, item_id: str, *, commit: bool = True, **data,
):
    repo = repo_class(db)
    obj = repo.get(novel_id, item_id)
    try:
        for key, value in data.items():
            if value is not None:
                setattr(obj, key, value)
        db.flush()
        _replace_index(db, novel_id, obj)
        if isinstance(obj, models.Character):
            _refresh_character_relationships(db, novel_id, obj.id)
        if commit:
            db.commit()
            db.refresh(obj)
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
        db.delete(obj)
        if isinstance(obj, models.Character):
            db.flush()
            _refresh_character_relationships(db, novel_id, item_id)
        db.commit()
    except Exception:
        db.rollback()
        raise


def _refresh_character_relationships(db: Session, novel_id: str, character_id: str):
    relationships = db.query(models.Relationship).filter(
        models.Relationship.novel_id == novel_id,
        (models.Relationship.subject_id == character_id)
        | (models.Relationship.object_id == character_id),
    ).all()
    for relationship in relationships:
        _replace_index(db, novel_id, relationship)


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
        indexed = 0
        failed = 0
        for repo_class in classes:
            for obj in repo_class(db).list(novel_id):
                if _replace_index(db, novel_id, obj):
                    indexed += 1
                else:
                    failed += 1
        db.commit()
        return {"indexed": indexed, "failed": failed}
    except Exception:
        db.rollback()
        raise
