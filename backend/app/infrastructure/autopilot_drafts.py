"""自动驾驶半稿恢复仓储；不写章节、设定、记忆或历史版本。"""
from threading import RLock
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.infrastructure import models


draft_write_lock = RLock()
RECOVERED_REASON = "恢复的生成片段，章节保存状态尚未确认"
_AUTHOR_FIELDS = ("node_id", "title", "attempt", "content", "reason", "state")


class DraftStorageError(Exception):
    def __init__(self, status_code: int, detail: str, code: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.code = code


class AutopilotDraftRepo:
    """单独会话提交恢复缓存，版本号和墓碑防止覆盖及复活。

    生成用例须传专用缓存会话，不能提交尚未完成的章节事务。
    """

    def __init__(self, db: Session):
        self.db = db

    def _ensure_novel(self, novel_id: str):
        if self.db.get(models.Novel, novel_id) is None:
            raise DraftStorageError(404, "作品不存在", "novel_not_found")

    @staticmethod
    def _id(draft_id: str):
        try:
            return str(UUID(draft_id))
        except (ValueError, TypeError, AttributeError):
            raise DraftStorageError(422, "草稿 ID 必须是 UUID", "draft_invalid") from None

    def _get(self, novel_id: str, draft_id: str):
        obj = self.db.get(models.AutopilotDraft, draft_id, populate_existing=True)
        if obj is not None and obj.novel_id != novel_id:
            raise DraftStorageError(404, "草稿不存在", "draft_not_found")
        return obj

    def get_raw(self, novel_id: str, draft_id: str):
        """供后台生命周期检查使用；此处不把 active 映射为未确认。"""
        self._ensure_novel(novel_id)
        return self._get(novel_id, self._id(draft_id))

    @staticmethod
    def read(obj, *, recover: bool = False):
        result = {
            key: getattr(obj, key)
            for key in ("id", "novel_id", *_AUTHOR_FIELDS, "revision", "created_at", "updated_at")
        }
        if recover and obj.state == "active":
            result["state"] = "unconfirmed"
            result["reason"] = obj.reason or RECOVERED_REASON
        return result

    def list(self, novel_id: str):
        self._ensure_novel(novel_id)
        rows = self.db.query(models.AutopilotDraft).filter(
            models.AutopilotDraft.novel_id == novel_id,
            models.AutopilotDraft.deleted.is_(False),
        ).order_by(models.AutopilotDraft.created_at, models.AutopilotDraft.id).all()
        return [self.read(row, recover=True) for row in rows]

    def next_attempt(self, novel_id: str) -> int:
        self._ensure_novel(novel_id)
        maximum = self.db.query(func.max(models.AutopilotDraft.attempt)).filter(
            models.AutopilotDraft.novel_id == novel_id,
        ).scalar()
        return (maximum or 0) + 1

    def upsert(self, novel_id: str, draft_id: str, **data):
        # 校验同样适用于生成用例，避免非接口调用绕过字段约束。
        payload = {"title": "", "reason": "", **data}
        if set(payload) != {*_AUTHOR_FIELDS, "revision"}:
            raise DraftStorageError(422, "草稿字段不完整或包含未知字段", "draft_invalid")
        for key, limit in (("node_id", 100), ("title", 200), ("content", 1_000_000), ("reason", 4000)):
            if type(payload[key]) is not str or len(payload[key]) > limit:
                raise DraftStorageError(422, "草稿文字字段不合法", "draft_invalid")
        if not payload["node_id"].strip() or payload["state"] not in ("active", "failed", "unconfirmed"):
            raise DraftStorageError(422, "草稿节点或保存状态不合法", "draft_invalid")
        if any(type(payload[key]) is not int or not 1 <= payload[key] <= 2_147_483_647
               for key in ("attempt", "revision")):
            raise DraftStorageError(422, "草稿尝试编号和版本号须为正整数", "draft_invalid")
        draft_id = self._id(draft_id)
        with draft_write_lock:
            try:
                self._ensure_novel(novel_id)
                obj = self._get(novel_id, draft_id)
                if obj is not None:
                    if obj.deleted:
                        raise DraftStorageError(410, "草稿已丢弃，不能再次保存", "draft_deleted")
                    if payload["revision"] < obj.revision:
                        raise DraftStorageError(409, "已有较新草稿，请勿覆盖", "draft_stale")
                    if payload["revision"] == obj.revision:
                        if any(getattr(obj, key) != payload[key] for key in _AUTHOR_FIELDS):
                            raise DraftStorageError(409, "相同版本的草稿内容不一致", "draft_stale")
                        return self.read(obj)
                    if obj.node_id != payload["node_id"] or obj.attempt != payload["attempt"]:
                        raise DraftStorageError(409, "草稿不能移到其他节点或生成尝试", "draft_identity_conflict")
                    for key, value in payload.items():
                        setattr(obj, key, value)
                else:
                    obj = models.AutopilotDraft(id=draft_id, novel_id=novel_id, **payload)
                    self.db.add(obj)
                self.db.commit()
                self.db.refresh(obj)
                return self.read(obj)
            except Exception:
                self.db.rollback()
                raise

    def delete(self, novel_id: str, draft_id: str, revision: int | None = None):
        draft_id = self._id(draft_id)
        if revision is not None and (type(revision) is not int or not 1 <= revision <= 2_147_483_647):
            raise DraftStorageError(422, "草稿版本号必须为正整数", "draft_invalid")
        with draft_write_lock:
            try:
                self._ensure_novel(novel_id)
                obj = self._get(novel_id, draft_id)
                if obj is None:
                    # 先删除后到达的保存也必须被挡住；正文不进入墓碑。
                    obj = models.AutopilotDraft(
                        id=draft_id, novel_id=novel_id, deleted=True,
                        revision=revision or 1, state="unconfirmed",
                    )
                    self.db.add(obj)
                elif not obj.deleted:
                    obj.deleted = True
                    obj.content = ""
                    obj.reason = ""
                    obj.title = ""
                    obj.revision = max(obj.revision + 1, revision or 1)
                self.db.commit()
                self.db.refresh(obj)
                return {"id": obj.id, "deleted": True, "revision": obj.revision}
            except Exception:
                self.db.rollback()
                raise
