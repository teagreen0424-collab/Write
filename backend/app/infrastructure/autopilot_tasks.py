"""后台任务与公开事件仓储；快照和游标在同一 SQLite 事务提交。"""
from copy import deepcopy
import hashlib
import json
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.application.word_count import normalize_word_limits
from app.infrastructure import models
from app.infrastructure.autopilot_drafts import draft_write_lock


ACTIVE_STATUSES = frozenset({"starting", "running", "paused"})
TERMINAL_STATUSES = frozenset({"completed", "incomplete", "stopped", "interrupted"})
EVENT_TYPES = frozenset({"progress", "text", "check", "story_check", "word_check", "chapter_done", "tension", "drift",
                         "paused", "error", "done", "control"})


class TaskStorageError(Exception):
    def __init__(self, status_code: int, detail: str, code: str, task_id: str | None = None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.code = code
        self.task_id = task_id


def task_uuid(value) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise TaskStorageError(422, "任务 ID 必须是 UUID", "task_invalid") from None


def injection_identity(request_id, directive):
    """原文就是实际执行载荷，不按去空白或相同文字合并作者操作。"""
    try:
        request_id = str(UUID(str(request_id)))
        if not isinstance(directive, str) or not directive.strip():
            raise ValueError
        digest = hashlib.sha256(directive.encode("utf-8")).hexdigest()
    except (ValueError, TypeError, AttributeError, UnicodeError):
        raise TaskStorageError(422, "构思请求身份或原文不合法", "autopilot_injection_invalid") from None
    return request_id, digest


def initial_snapshot(novel_id: str, task_id: str, word_limits=None):
    return {
        "type": "snapshot", "task_id": task_id, "novel_id": novel_id, "event_id": 0,
        "status": "starting", "running": True, "paused": False, "pause_requested": False,
        "generated": 0, "skipped": 0, "failed": 0, "done": 0, "total": 0,
        "current_node_id": "", "current_seq": 0, "message": "自动驾驶已启动，等待后台执行",
        "task_notice": "", "terminal_reason": "", "active_drafts": [],
        "quality_warnings": [], "drift_warnings": [],
        "word_limits": deepcopy(word_limits),
    }


def project_event(snapshot, event):
    """只投影已提交事件，不能把较新的原始缓存误当成当前游标内容。"""
    result = deepcopy(snapshot)
    result["event_id"] = event["event_id"]
    for key in ("generated", "skipped", "failed", "done", "total"):
        if key in event:
            result[key] = event[key]
    kind = event["type"]
    node_id = event.get("node_id")
    if node_id:
        result["current_node_id"] = node_id
    if "seq" in event:
        result["current_seq"] = event["seq"]
    if kind == "progress":
        result["message"] = event.get("message", result["message"])
        if event.get("phase") == "generating":
            result.update(status="running", paused=False)
    if kind in ("progress", "text") and node_id and (
        kind == "text" or event.get("phase") == "generating"
    ):
        draft_id = event.get("draft_id", "")
        draft = next((item for item in result["active_drafts"] if item["node_id"] == node_id
                      and item["draft_id"] == draft_id), None)
        if draft is None:
            result["active_drafts"] = [item for item in result["active_drafts"] if item["node_id"] != node_id]
            draft = {"node_id": node_id, "title": event.get("title", ""), "content": "",
                     "draft_id": draft_id, "draft_revision": 0, "draft_attempt": 1,
                     "draft_persisted": False}
            result["active_drafts"].append(draft)
        for key in ("draft_revision", "draft_attempt", "draft_persisted"):
            if key in event:
                draft[key] = event[key]
        if kind == "text":
            if not isinstance(event.get("content"), str):
                raise TaskStorageError(422, "任务正文事件必须含字符串内容", "task_event_invalid")
            draft["content"] += event["content"]
    elif kind == "chapter_done" or (kind == "error" and event.get("scope") == "chapter"):
        result["active_drafts"] = [item for item in result["active_drafts"] if
                                   not (item["node_id"] == node_id and
                                        (not event.get("draft_id") or item["draft_id"] == event["draft_id"]))]
    if kind == "error":
        message = event.get("message", "任务发生错误")
        if event.get("scope") == "quality" or (event.get("saved") and not event.get("fatal")):
            if message not in result["quality_warnings"]:
                result["quality_warnings"].append(message)
        elif event.get("scope") == "task" or event.get("fatal"):
            result["task_notice"] = message
    elif kind in ("check", "story_check", "word_check"):
        warnings = event.get("warnings", [])
        if not isinstance(warnings, list) or any(not isinstance(w, str) for w in warnings):
            raise TaskStorageError(422, "质量警告必须是字符串列表", "task_event_invalid")
        for warning in warnings:
            if warning and warning not in result["quality_warnings"]:
                result["quality_warnings"].append(warning)
    elif kind == "drift" and event.get("warning"):
        result["drift_warnings"].append({
            "seq": event["seq"], "score": event["score"],
            "message": event.get("message", f"第{event['seq']}章文风漂移（相似度 {event['score']}）"),
        })
    elif kind == "paused":
        result.update(status="paused", paused=True, pause_requested=False,
                      message=event.get("message", "已暂停"))
    elif kind == "control":
        if event.get("action") in ("pause", "resume"):
            result["paused"] = bool(event.get("paused"))
            result["pause_requested"] = bool(event.get("pause_requested"))
            if result["paused"]:
                result["status"] = "paused"
            elif event["action"] == "resume" and result["status"] == "paused":
                result["status"] = "running"
        result["message"] = event.get("message", result["message"])
    elif kind == "done":
        status = event.get("status", "completed")
        if status not in TERMINAL_STATUSES:
            raise TaskStorageError(422, "任务终态不合法", "task_event_invalid")
        result.update(status=status, running=False, paused=False, pause_requested=False,
                      active_drafts=[], message=event.get("message", "自动驾驶结束"),
                      terminal_reason=event.get("reason", ""))
    return result


class AutopilotTaskRepo:
    """每次操作使用任务专用会话；不得提交尚未完成的章节事务。"""

    def __init__(self, db: Session):
        self.db = db

    def _ensure_novel(self, novel_id):
        if self.db.get(models.Novel, novel_id) is None:
            raise TaskStorageError(404, "作品不存在", "novel_not_found")

    def _task(self, novel_id, task_id):
        self._ensure_novel(novel_id)
        obj = self.db.get(models.AutopilotTask, task_uuid(task_id), populate_existing=True)
        if obj is None or obj.novel_id != novel_id:
            raise TaskStorageError(404, "任务不存在", "task_not_found")
        return obj

    def get(self, novel_id, task_id):
        return deepcopy(self._task(novel_id, task_id).snapshot_json)

    def latest(self, novel_id):
        self._ensure_novel(novel_id)
        task = self.db.query(models.AutopilotTask).filter(
            models.AutopilotTask.novel_id == novel_id,
        ).order_by(models.AutopilotTask.created_at.desc(), models.AutopilotTask.id.desc()).first()
        return deepcopy(task.snapshot_json) if task is not None else None

    def create(self, novel_id, request_id, word_limits=None):
        request_id = task_uuid(request_id)
        try:
            word_limits = normalize_word_limits(word_limits)
        except ValueError as error:
            raise TaskStorageError(422, str(error), "autopilot_invalid") from None
        with draft_write_lock:
            try:
                self._ensure_novel(novel_id)
                existing = self.db.get(models.AutopilotTask, request_id, populate_existing=True)
                if existing is not None:
                    if existing.novel_id != novel_id:
                        raise TaskStorageError(404, "任务不存在", "task_not_found")
                    if existing.snapshot_json.get("word_limits") != word_limits:
                        raise TaskStorageError(409, "该请求 ID 已用于不同的篇幅范围，请使用新的请求 ID", "autopilot_request_conflict", existing.id)
                    return deepcopy(existing.snapshot_json), False
                active = self.db.query(models.AutopilotTask).filter(
                    models.AutopilotTask.novel_id == novel_id,
                    models.AutopilotTask.status.in_(ACTIVE_STATUSES),
                ).first()
                if active is not None:
                    raise TaskStorageError(409, "该作品已有自动驾驶任务在运行", "autopilot_busy", active.id)
                snapshot = initial_snapshot(novel_id, request_id, word_limits)
                self.db.add(models.AutopilotTask(
                    id=request_id, novel_id=novel_id, status="starting", snapshot_json=snapshot,
                ))
                self.db.commit()
                return deepcopy(snapshot), True
            except Exception:
                self.db.rollback()
                raise

    def _append_to_task(self, task, novel_id, event):
        if task.status in TERMINAL_STATUSES:
            raise TaskStorageError(409, "任务已经结束", "task_finished", task.id)
        public = {**deepcopy(event), "task_id": task.id, "event_id": task.last_event_id + 1}
        json.dumps(public, ensure_ascii=False, allow_nan=False)
        snapshot = project_event(task.snapshot_json, public)
        task.status = snapshot["status"]
        task.last_event_id = public["event_id"]
        task.snapshot_json = snapshot
        if task.status in TERMINAL_STATUSES:
            task.finished_at = models._now()
        self.db.add(models.AutopilotTaskEvent(
            task_id=task.id, novel_id=novel_id,
            event_id=public["event_id"], data_json=public,
        ))
        return public

    def append(self, novel_id, task_id, event):
        if (not isinstance(event, dict) or not isinstance(event.get("type"), str)
                or event["type"] not in EVENT_TYPES):
            raise TaskStorageError(422, "任务事件不合法", "task_event_invalid")
        with draft_write_lock:
            try:
                task = self._task(novel_id, task_id)
                public = self._append_to_task(task, novel_id, event)
                self.db.commit()
                return deepcopy(public)
            except Exception:
                self.db.rollback()
                raise

    def _injection_event(self, request_id):
        table = models.AutopilotTaskEvent.__table__
        return self.db.execute(select(table.c.novel_id, table.c.task_id, table.c.data_json).where(
            table.c.data_json["type"].as_string() == "control",
            table.c.data_json["action"].as_string() == "inject",
            table.c.data_json["request_id"].as_string() == request_id,
        )).mappings().first()

    @staticmethod
    def _injection_replay(row, novel_id, task_id, directive, digest):
        public = row["data_json"]
        if (row["novel_id"] != novel_id or row["task_id"] != task_id
                or public.get("directive_hash") != digest or public.get("directive") != directive):
            raise TaskStorageError(409, "该构思请求已用于不同任务或内容，请核对原请求",
                                   "autopilot_injection_request_conflict", task_id)
        return {**deepcopy(public), "receipt_replayed": True}

    def find_injection(self, novel_id, task_id, request_id, directive):
        request_id, digest = injection_identity(request_id, directive)
        task_id = self._task(novel_id, task_id).id  # 先验证作品和任务，不能泄露另一作品原文。
        row = self._injection_event(request_id)
        return self._injection_replay(row, novel_id, task_id, directive, digest) if row else None

    def injection_receipt(self, novel_id, task_id, request_id):
        task_id = self._task(novel_id, task_id).id
        request_id = task_uuid(request_id)
        row = self._injection_event(request_id)
        if row is None or row["novel_id"] != novel_id or row["task_id"] != task_id:
            raise TaskStorageError(404, "该任务尚无此构思请求记录", "autopilot_injection_not_found", task_id)
        return {**deepcopy(row["data_json"]), "receipt_replayed": True}

    def accept_injection(self, novel_id, task_id, request_id, directive, event):
        """任务专用会话内短写事务查重和记账；不触碰章节或内存执行状态。"""
        request_id, digest = injection_identity(request_id, directive)
        # 拒绝调用方DML在try外，不能rollback已flush/Core事务或其未保存作者稿。
        if self.db.new or self.db.dirty or self.db.deleted:
            raise RuntimeError("构思回执写入必须使用无待提交修改的任务专用会话")
        if self.db.in_transaction() and self.db.get_bind().dialect.name == "sqlite":
            if self.db.connection().connection.driver_connection.in_transaction:
                raise RuntimeError("构思回执写入不能提交调用方已有写事务")
        with draft_write_lock:
            try:
                self.db.rollback()  # 只释放只读autobegin；调用方DML已经在上方拒绝。
                if self.db.get_bind().dialect.name == "sqlite":
                    self.db.execute(text("BEGIN IMMEDIATE"))
                task = self._task(novel_id, task_id)
                previous = self._injection_event(request_id)
                if previous is not None:
                    result = self._injection_replay(previous, novel_id, task.id, directive, digest)
                    self.db.rollback()  # 并发原请求已提交，本次未写入，只释放写锁。
                    return result, False
                public = self._append_to_task(task, novel_id, {
                    **event, "novel_id": novel_id, "type": "control", "action": "inject",
                    "request_id": request_id, "directive": directive, "directive_hash": digest,
                    "accepted": True, "receipt_replayed": False,
                    "message": "构思已记录，请核对实际章节执行结果",
                })
                # public在commit前冻结，提交成功后无refresh或其它SQL读取。
                result = deepcopy(public)
                self.db.commit()
                return result, True
            except Exception:
                self.db.rollback()
                raise

    def read_batch(self, novel_id, task_id, after: int, limit: int = 128):
        task = self._task(novel_id, task_id)
        cursor = task.last_event_id
        if type(after) is not int or after < 0 or after > cursor:
            raise TaskStorageError(422, "任务游标超出已保存事件范围", "task_cursor_invalid", task.id)
        events = self.db.query(models.AutopilotTaskEvent).filter(
            models.AutopilotTaskEvent.novel_id == novel_id,
            models.AutopilotTaskEvent.task_id == task.id,
            models.AutopilotTaskEvent.event_id > after,
            models.AutopilotTaskEvent.event_id <= cursor,
        ).order_by(models.AutopilotTaskEvent.event_id).limit(limit).all()
        return deepcopy(task.snapshot_json), [deepcopy(item.data_json) for item in events]

    def terminal_event(self, novel_id, task_id):
        task = self._task(novel_id, task_id)
        last = self.db.query(models.AutopilotTaskEvent).filter(
            models.AutopilotTaskEvent.task_id == task.id,
            models.AutopilotTaskEvent.data_json["type"].as_string() == "done",
        ).order_by(models.AutopilotTaskEvent.event_id.desc()).first()
        return deepcopy(last.data_json) if last is not None else None

    def active_tasks(self):
        return [(item.novel_id, item.id) for item in self.db.query(models.AutopilotTask).filter(
            models.AutopilotTask.status.in_(ACTIVE_STATUSES),
        ).all()]

    def interrupt(self, novel_id, task_id, message, code="backend_restarted"):
        with draft_write_lock:
            snapshot = self.get(novel_id, task_id)
            if snapshot["status"] in TERMINAL_STATUSES:
                return snapshot
            self.append(novel_id, task_id, {
                "type": "error", "scope": "task", "fatal": True, "saved": False,
                "code": code, "message": message,
            })
            self.append(novel_id, task_id, {
                "type": "done", "status": "interrupted", "reason": code, "message": message,
            })
            return self.get(novel_id, task_id)

    def recover_active_drafts(self, novel_id):
        with draft_write_lock:
            try:
                rows = self.db.query(models.AutopilotDraft).filter(
                    models.AutopilotDraft.novel_id == novel_id,
                    models.AutopilotDraft.deleted.is_(False), models.AutopilotDraft.state == "active",
                ).all()
                for row in rows:
                    row.state = "unconfirmed"
                    row.reason = "后台服务已重启，上次生成已中断，章节保存状态尚未确认；请核对章节。"
                    row.revision += 1
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
