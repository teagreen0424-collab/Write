"""独立后台执行与 SQLite 事件订阅；浏览器连接只观察，不拥有生成器。"""
from copy import deepcopy
from dataclasses import dataclass
import json
import threading
import time

from app.application import auto_pilot
from app.infrastructure.autopilot_drafts import draft_write_lock
from app.infrastructure.autopilot_tasks import (
    ACTIVE_STATUSES, TERMINAL_STATUSES, AutopilotTaskRepo, TaskStorageError, task_uuid,
)
from app.infrastructure.db import SessionLocal


def _sse(event):
    return f"id: {event['event_id']}\ndata: {json.dumps(event, ensure_ascii=False, allow_nan=False)}\n\n"


class _WorkerStopped(Exception):
    pass


@dataclass
class _Worker:
    novel_id: str
    task_id: str
    state: object
    thread: threading.Thread | None = None


class AutopilotTaskManager:
    def __init__(self, session_factory=SessionLocal, runner=None, registry=None,
                 heartbeat_seconds=0.5, shutdown_timeout=2.0):
        self.session_factory = session_factory
        self.runner = runner or auto_pilot.run_autopilot
        self.registry = registry or auto_pilot.registry
        self.heartbeat_seconds = min(1.0, max(0.01, heartbeat_seconds))
        self.shutdown_timeout = max(0.0, shutdown_timeout)
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        # 不同作品可排队，生成执行仍串行，不制造多个自动驾驶 SQLite 写者。
        self._execution_gate = threading.Lock()
        self._workers: dict[str, _Worker] = {}
        self._fallback: dict[str, dict] = {}
        self._closing = False

    def _read(self, operation, *args):
        with self.session_factory() as db:
            return getattr(AutopilotTaskRepo(db), operation)(*args)

    def _snapshot(self, snapshot):
        if snapshot is None:
            return None
        with self._lock:
            return deepcopy(self._fallback.get(snapshot["task_id"], snapshot))

    def status(self, novel_id):
        return self._snapshot(self._read("latest", novel_id))

    def get(self, novel_id, task_id):
        return self._snapshot(self._read("get", novel_id, task_id))

    def start(self, novel_id, request_id, word_limits=None):
        request_id = task_uuid(request_id)
        with self._condition:
            if self._closing:
                raise TaskStorageError(503, "后台服务正在关闭，请稍后重试", "task_service_stopping")
            # 临时写入故障结束了执行，但旧 active 行可能尚未更新；恢复写入后先收尾旧身份。
            for fallback_id, fallback in list(self._fallback.items()):
                if fallback["novel_id"] != novel_id or fallback_id in self._workers:
                    continue
                try:
                    self._read("interrupt", novel_id, fallback_id,
                               fallback["message"], fallback["terminal_reason"])
                except Exception as exc:
                    raise TaskStorageError(
                        503, "上次任务的中断状态仍无法写入数据库，请稍后重试", "task_storage_unavailable", fallback_id,
                    ) from exc
                self._fallback.pop(fallback_id, None)
            with self.session_factory() as db:
                snapshot, created = AutopilotTaskRepo(db).create(novel_id, request_id, word_limits)
            if not created:
                return self._snapshot(snapshot)
            try:
                state = self.registry.start(novel_id, owner_id=request_id, word_limits=snapshot.get("word_limits"))
                worker = _Worker(novel_id, request_id, state)
                worker.thread = threading.Thread(
                    target=self._execute, args=(worker,), name=f"autopilot-{request_id}", daemon=True,
                )
                self._workers[request_id] = worker
                worker.thread.start()
            except Exception as exc:
                self._workers.pop(request_id, None)
                self.registry.finish(novel_id, owner_id=request_id)
                self._interrupt(novel_id, request_id, f"后台任务启动失败：{str(exc)[:500]}", "task_start_failed")
                raise TaskStorageError(503, "后台任务未能启动，请核对任务状态", "task_start_failed", request_id) from exc
            return snapshot

    @staticmethod
    def _decode(frame):
        if isinstance(frame, dict):
            return frame
        if not isinstance(frame, str):
            raise ValueError("后台生成事件不是 SSE 数据")
        pieces = [line[5:].lstrip(" ") for line in frame.splitlines() if line.startswith("data:")]
        if not pieces:
            return None  # 内核暂停心跳不写成任务事件。
        event = json.loads("\n".join(pieces))
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ValueError("后台生成事件缺少合法 type")
        return event

    def _publish(self, worker, event):
        with self._condition:
            if self._closing:
                raise _WorkerStopped()
            public = self._read("append", worker.novel_id, worker.task_id, event)
            self._condition.notify_all()
            return public

    def _interrupt(self, novel_id, task_id, message, code):
        """持久化不可用时也不能在当前进程谎称任务仍在执行。"""
        try:
            snapshot = self._read("interrupt", novel_id, task_id, message, code)
        except Exception:
            try:
                snapshot = self._read("get", novel_id, task_id)
            except Exception:
                snapshot = self._fallback.get(task_id)
            if snapshot is None:
                return
            snapshot = deepcopy(snapshot)
            snapshot.update(status="interrupted", running=False, paused=False, pause_requested=False,
                            active_drafts=[], message=message,
                            task_notice=message + " 任务终态无法写入数据库，请先核对暂存片段。",
                            terminal_reason=code)
            self._fallback[task_id] = snapshot
        self._condition.notify_all()

    def _execute(self, worker):
        acquired = False
        stream = None
        completed = False
        error = None
        try:
            while not acquired:
                with self._lock:
                    if self._closing:
                        raise _WorkerStopped()
                acquired = self._execution_gate.acquire(timeout=self.heartbeat_seconds)
            with self._lock:
                if self._closing:
                    raise _WorkerStopped()
            stream = iter(self.runner(worker.novel_id, session_factory=self.session_factory,
                                      reserved_state=worker.state))
            for frame in stream:
                with self._lock:
                    if self._closing:
                        raise _WorkerStopped()
                event = self._decode(frame)
                if event is None:
                    continue
                self._publish(worker, event)
                if event["type"] == "done":
                    completed = True
                    break
            if not completed:
                raise RuntimeError("后台生成未发出任务结束确认")
        except _WorkerStopped:
            pass
        except Exception as exc:
            error = f"后台任务执行已中断：{str(exc)[:500]} 请核对章节和暂存片段后重试。"
            worker.state.stop = True
        finally:
            if stream is not None and hasattr(stream, "close"):
                try:
                    stream.close()
                except Exception as exc:
                    error = error or f"后台任务收尾失败：{str(exc)[:500]}"
            if acquired:
                self._execution_gate.release()
            with self._condition:
                if error is not None and not self._closing:
                    self._interrupt(worker.novel_id, worker.task_id, error, "task_execution_interrupted")
                self.registry.finish(worker.novel_id, owner_id=worker.task_id)
                self._workers.pop(worker.task_id, None)
                self._condition.notify_all()

    def subscribe(self, novel_id, task_id, after=None):
        # 在返回 StreamingResponse 前完成验证，坏游标/作用域应返回普通 HTTP 错误。
        initial = self.get(novel_id, task_id)
        if after is not None and (type(after) is not int or after < 0 or after > initial["event_id"]):
            raise TaskStorageError(422, "任务游标超出已保存事件范围", "task_cursor_invalid", initial["task_id"])

        def frames():
            cursor = initial["event_id"] if after is None else after
            if after is None:
                yield _sse(initial)
            while True:
                # 此方法已关闭会话，随后才 yield 或等待；慢客户端不占读事务。
                snapshot, events = self._read("read_batch", novel_id, task_id, cursor)
                snapshot = self._snapshot(snapshot)
                for event in events:
                    cursor = event["event_id"]
                    yield _sse(event)
                    if event["type"] == "done":
                        return
                if cursor < snapshot["event_id"]:
                    if not events:
                        raise TaskStorageError(503, "任务事件记录不连续，无法继续重放", "task_replay_incomplete", task_id)
                    # 终态可能位于后续批次，不能越过仍未重放的正文直接发送 done。
                    continue
                if snapshot["status"] in TERMINAL_STATUSES:
                    terminal = self._read("terminal_event", novel_id, task_id)
                    if terminal is None:
                        terminal = {
                            "type": "done", "task_id": snapshot["task_id"], "event_id": snapshot["event_id"],
                            "status": snapshot["status"], "message": snapshot["message"],
                            "reason": snapshot["terminal_reason"],
                            **{key: snapshot[key] for key in ("generated", "skipped", "failed", "done", "total")},
                        }
                    # 即使客户端游标已包括终态，也返回同游标的确认，避免裸 EOF。
                    yield _sse(terminal)
                    return
                with self._condition:
                    self._condition.wait(timeout=self.heartbeat_seconds)
                yield ": heartbeat\n\n"

        return frames()

    def _control(self, novel_id, task_id, action, directive=None):
        task_id = task_uuid(task_id)
        with self._condition, draft_write_lock:
            if self._closing:
                raise TaskStorageError(503, "后台服务正在关闭", "task_service_stopping", task_id)
            snapshot = self.get(novel_id, task_id)
            state = self.registry.get(novel_id)
            if snapshot["status"] not in ACTIVE_STATUSES or state is None or state.owner_id != task_id:
                raise TaskStorageError(409, "该任务已经结束或不再拥有当前执行", "task_not_active", task_id)
            event = {"type": "control", "action": action, "paused": snapshot["paused"],
                     "pause_requested": snapshot["pause_requested"]}
            if action == "pause":
                event.update(paused=snapshot["paused"], pause_requested=not snapshot["paused"],
                             message="已暂停" if snapshot["paused"] else "已请求暂停，当前章完成后暂停")
            elif action == "resume":
                event.update(paused=False, pause_requested=False, message="已恢复自动驾驶")
            else:
                if not isinstance(directive, str) or not directive.strip():
                    raise TaskStorageError(422, "章前指令不能为空", "task_invalid", task_id)
                event["message"] = "已接收章前指令，将在下一章应用"
            # 先提交公开确认，再修改内存控制，不能因事件写入失败悄悄改变运行状态。
            public = self._read("append", novel_id, task_id, event)
            if action == "pause":
                applied = self.registry.pause(novel_id, owner_id=task_id)
            elif action == "resume":
                applied = self.registry.resume(novel_id, owner_id=task_id)
            else:
                applied = self.registry.inject(novel_id, directive, owner_id=task_id)
            if applied is None:
                raise TaskStorageError(409, "任务已经结束，请重新读取状态", "task_not_active", task_id)
            self._condition.notify_all()
            return public

    def pause(self, novel_id, task_id):
        return self._control(novel_id, task_id, "pause")

    def resume(self, novel_id, task_id):
        return self._control(novel_id, task_id, "resume")

    def inject(self, novel_id, task_id, directive, request_id=None):
        if request_id is None:
            return self._control(novel_id, task_id, "inject", directive)
        task_id = task_uuid(task_id)
        with self._condition, draft_write_lock:
            # 历史回执优先关闭/终态/registry判断；重放永远不再次安装构思。
            previous = self._read("find_injection", novel_id, task_id, request_id, directive)
            if previous is not None:
                return previous
            if self._closing:
                raise TaskStorageError(503, "后台服务正在关闭", "task_service_stopping", task_id)
            snapshot = self.get(novel_id, task_id)
            state = self.registry.get(novel_id)
            if snapshot["status"] not in ACTIVE_STATUSES or state is None or state.owner_id != task_id:
                raise TaskStorageError(409, "该任务已经结束或不再拥有当前执行", "task_not_active", task_id)
            public, created = self._read("accept_injection", novel_id, task_id, request_id, directive, {
                "paused": snapshot["paused"], "pause_requested": snapshot["pause_requested"],
            })
            if created:
                # durable accepted仅证明已记录。落库后内存派发失败不能否认原回执；
                # 崩溃/结束后的历史查重也绝不重新派发。实际结果由作者核对章节。
                try:
                    self.registry.inject(novel_id, directive, owner_id=task_id)
                except Exception:
                    pass
                self._condition.notify_all()
            return public

    def injection_receipt(self, novel_id, task_id, request_id):
        return self._read("injection_receipt", novel_id, task_id, request_id)

    def recover_interrupted(self):
        with self._condition, draft_write_lock:
            if self._workers:
                raise TaskStorageError(409, "当前进程仍有后台任务，不能执行启动恢复", "task_recovery_busy")
            tasks = self._read("active_tasks")
            for novel_id, task_id in tasks:
                self._read("recover_active_drafts", novel_id)
                self._read("interrupt", novel_id, task_id,
                           "后台服务已重启，上次任务已中断；请核对已保存章节与暂存片段后重试。",
                           "backend_restarted")
                self._fallback.pop(task_id, None)
            # 同一 FastAPI 对象可重复进入 lifespan；仅在旧线程全部退出后重新开放。
            self._closing = False
            self._condition.notify_all()
            return len(tasks)

    def shutdown(self):
        with self._condition:
            self._closing = True
            workers = list(self._workers.values())
            for worker in workers:
                worker.state.stop = True
                worker.state.paused = False
                self._interrupt(worker.novel_id, worker.task_id,
                                "后台服务关闭，任务已中断；已保存章节和暂存片段仍可核对。",
                                "backend_shutdown")
            self._condition.notify_all()
        deadline = time.monotonic() + self.shutdown_timeout
        for worker in workers:
            if worker.thread is not None and worker.thread is not threading.current_thread():
                worker.thread.join(timeout=max(0.0, deadline - time.monotonic()))

    def wait(self, task_id, timeout=5.0):
        task_id = task_uuid(task_id)
        with self._lock:
            worker = self._workers.get(task_id)
        if worker is None or worker.thread is None:
            return True
        worker.thread.join(timeout=timeout)
        return not worker.thread.is_alive()


manager = AutopilotTaskManager()
