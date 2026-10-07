"""按原稿与规范化建议保存确认记录，重试只重放已经提交的逐项结果。"""
import hashlib
import json
import re

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.application import setting_index
from app.application.reference_resolution import failure, resolve_references
from app.application.revisions import _require_clean_session
from app.infrastructure import models, repository as repos
from app.interfaces import schemas

_SCHEMAS = {
    "character": schemas.CharacterCreate,
    "timeline_event": schemas.TimelineEventCreate,
    "foreshadowing": schemas.ForeshadowingCreate,
}
_REPOS = {
    "character": repos.CharacterRepo,
    "timeline_event": repos.TimelineEventRepo,
    "foreshadowing": repos.ForeshadowingRepo,
}


class SettleSourceChanged(HTTPException):
    code = "settle_source_changed"

    def __init__(self):
        super().__init__(409, detail="章节内容、标题或指令已变化；尚未确认的旧稿建议未写入，请重新定稿最新稿")


class SettleNameExists(HTTPException):
    code = "settle_name_exists"

    def __init__(self, value: str, existing_ids: list[str]):
        self.existing_ids = existing_ids
        super().__init__(409, detail=f"同类型设定“{value}”已存在（ID：{'、'.join(existing_ids)}）；请到设定面板核对，未自动合并或创建")


def _digest(value: dict) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def chapter_source_fingerprint(novel_id: str, chapter_id: str, source: dict) -> str:
    """精确作者字段，不把摘要、状态、评分与时间戳视为原稿变化。"""
    return _digest({
        "version": "chapter-settle-source-v1", "novel_id": novel_id, "chapter_id": chapter_id,
        **{field: source[field] for field in ("title", "content", "directive")},
    })


def _canonical_suggestion(suggestion: dict, chapter_id: str) -> tuple[str, dict, str]:
    if not isinstance(suggestion, dict):
        raise ValueError("建议必须是 JSON 对象")
    kind = suggestion.get("type")
    if not isinstance(kind, str) or kind not in _SCHEMAS:
        raise ValueError(f"不支持的设定类型：{kind}")
    data = suggestion.get("data")
    if not isinstance(data, dict):
        raise ValueError("建议 data 必须是 JSON 对象")
    # 先按有效字段及默认值规范化，再解析可能变化的名称/ID；理由和字典键顺序不影响确认身份。
    payload = _SCHEMAS[kind](**data).model_dump(mode="json")
    if kind == "foreshadowing" and not payload["planted_chapter_id"]:
        payload["planted_chapter_id"] = chapter_id
    fingerprint = _digest({"version": "chapter-settle-item-v1", "type": kind, "data": payload})
    return kind, payload, fingerprint


def _begin_item(db: Session):
    # 已由入口确认没有调用方未提交写入；只读autobegin可以安全释放并重读。
    db.rollback()
    if db.get_bind().dialect.name == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))
    db.expire_all()


def apply_with_receipts(
    db: Session, novel_id: str, chapter_id: str, suggestions: list[dict], expected_source: str,
) -> dict:
    """逐项事务包含实体、当前索引与receipt；已成功项不受后续失败影响。"""
    # 拒绝调用方写事务必须在try之外，不能把已flush/Core修改rollback丢掉。
    _require_clean_session(db)
    if not isinstance(expected_source, str) or re.fullmatch(r"[0-9a-f]{64}", expected_source) is None:
        raise HTTPException(422, detail="确认定稿建议必须提供有效的原稿指纹")
    repos.NovelRepo(db).get(novel_id)
    repos.ChapterRepo(db).get(novel_id, chapter_id)
    created, failed = [], []
    for index, suggestion in enumerate(suggestions):
        kind = suggestion.get("type", "") if isinstance(suggestion, dict) else ""
        try:
            kind, payload, item_hash = _canonical_suggestion(suggestion, chapter_id)
            _begin_item(db)
            chapter = db.query(models.Chapter).filter_by(novel_id=novel_id, id=chapter_id).with_for_update().first()
            if chapter is None:
                raise HTTPException(404, detail="章节不存在，尚未确认的建议未写入")
            receipt = db.query(models.ChapterSettleReceipt).filter_by(
                novel_id=novel_id, chapter_id=chapter_id, source_fingerprint=expected_source,
                suggestion_fingerprint=item_hash,
            ).first()
            if receipt is not None:
                exists = db.query(_REPOS[kind].model.id).filter_by(
                    novel_id=novel_id, id=receipt.entity_id,
                ).first() is not None
                saved = {"index": index, "type": kind, "id": receipt.entity_id,
                         "receipt_id": receipt.id, "replayed": True, "deleted": not exists}
                db.rollback()  # 重放不写库，也不重新解析可能已删除/更名/歧义的引用。
            else:
                current_source = {field: getattr(chapter, field) for field in ("title", "content", "directive")}
                if chapter_source_fingerprint(novel_id, chapter_id, current_source) != expected_source:
                    raise SettleSourceChanged()
                field = {"character": "name", "timeline_event": "title", "foreshadowing": "content"}[kind]
                value = payload[field]
                if value.strip():
                    model = _REPOS[kind].model
                    existing_ids = [row.id for row in db.query(model.id).filter(
                        model.novel_id == novel_id, getattr(model, field) == value,
                    ).order_by(model.id).all()]
                    if existing_ids:
                        raise SettleNameExists(value, existing_ids)
                resolved = resolve_references(db, novel_id, kind, payload)
                entity = setting_index.create_setting(db, _REPOS[kind], novel_id, commit=False, **resolved)
                receipt = models.ChapterSettleReceipt(
                    novel_id=novel_id, chapter_id=chapter_id, source_fingerprint=expected_source,
                    suggestion_fingerprint=item_hash, kind=kind, entity_id=entity.id, payload_json=payload,
                )
                db.add(receipt)
                db.flush()
                # 冻结提交后响应；提交成功后无refresh/过期属性读取，排他读锁不能否认已保存事实。
                saved = {"index": index, "type": kind, "id": entity.id,
                         "receipt_id": receipt.id, "replayed": False, "deleted": False}
                db.commit()
            created.append(saved)
        except Exception as error:  # noqa: BLE001 只回滚当前尚未成功的项目
            db.rollback()
            row = failure(index, str(kind or ""), error)
            if isinstance(error, (SettleSourceChanged, SettleNameExists)):
                row["code"] = error.code
            if isinstance(error, SettleNameExists):
                row["existing_ids"] = error.existing_ids
            failed.append(row)
    if not created and any(row.get("code") == SettleSourceChanged.code for row in failed):
        raise SettleSourceChanged()
    return {
        "created": created, "failed": failed,
        "new_count": sum(not row["replayed"] for row in created),
        "replayed_count": sum(row["replayed"] for row in created),
    }
