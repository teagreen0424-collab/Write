"""真实 SQLite 和受控线程：订阅断开不取消后台任务，游标重连不重复生成。"""
import json
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.application import auto_pilot, autopilot_tasks
from app.infrastructure import models
from app.infrastructure.autopilot_drafts import AutopilotDraftRepo
from app.infrastructure.autopilot_tasks import AutopilotTaskRepo
from app.infrastructure import repository as repos


def _event(frame):
    lines = [line[5:].strip() for line in frame.splitlines() if line.startswith("data:")]
    return json.loads("\n".join(lines)) if lines else None


class AutopilotTaskTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = (Path(directory.name) / "independent-tasks.db").as_posix()
        self.engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
        models.Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.sessions()
        self.addCleanup(self.db.close)
        self.novel_id = repos.NovelRepo(self.db).create(title="后台任务验收").id
        self.other_id = repos.NovelRepo(self.db).create(title="隔离作品").id
        self.registry = auto_pilot.AutoPilotRegistry()
        self.blocked = threading.Event()
        self.release = threading.Event()
        self.managers = []
        self.subscribers = []
        for mocked in (
            patch.object(auto_pilot, "registry", self.registry),
            patch.object(auto_pilot, "build_context", return_value="本机测试上下文"),
            patch.object(auto_pilot, "consistency_check", return_value={"conflicts": []}),
            patch.object(auto_pilot, "score_tension", return_value=5.0),
            patch.object(auto_pilot, "check_drift", return_value=None),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        # 清理最后注册，先释放所有线程/订阅，再停止patch与关闭SQLite连接。
        self.addCleanup(self._shutdown)

    def _shutdown(self):
        self.release.set()
        for novel_id in (self.novel_id, self.other_id):
            self.registry.resume(novel_id)
            state = self.registry.get(novel_id)
            if state is not None:
                state.stop = True
        for subscriber in self.subscribers:
            subscriber.close()
        for manager in reversed(self.managers):
            manager.shutdown()

    def _manager(self, runner=None, registry=None):
        manager = autopilot_tasks.AutopilotTaskManager(
            session_factory=self.sessions, runner=runner,
            registry=registry or self.registry, heartbeat_seconds=0.02, shutdown_timeout=2,
        )
        self.managers.append(manager)
        return manager

    def _nodes(self, count=1):
        return [repos.OutlineRepo(self.db).create(
            self.novel_id, level="chapter", title=f"后台第{index}章", sort_order=index,
        ) for index in range(1, count + 1)]

    def _gated_body(self, *args, **kwargs):
        yield "已经提交恢复缓存的第一段。"
        self.blocked.set()
        if not self.release.wait(5):
            raise RuntimeError("测试释放正文门闩超时")
        yield "后台独立完成的末段。"

    def _wait(self, condition, label):
        deadline = time.monotonic() + 5
        delay = threading.Event()
        while time.monotonic() < deadline:
            value = condition()
            if value:
                return value
            delay.wait(0.005)
        self.fail(f"等待{label}超时")

    def _terminal(self, manager, novel_id=None):
        novel_id = novel_id or self.novel_id
        initial = manager.status(novel_id)
        self.assertIsNotNone(initial)
        self.assertTrue(manager.wait(initial["task_id"], timeout=5), "后台线程未在期限内完成")
        terminal = manager.status(novel_id)
        self.assertFalse(terminal["running"])
        return terminal

    def _subscribe(self, manager, task_id, after=None, novel_id=None):
        subscriber = manager.subscribe(novel_id or self.novel_id, task_id, after=after)
        self.subscribers.append(subscriber)
        return subscriber

    def _until(self, subscriber, predicate):
        for _ in range(100):
            event = _event(next(subscriber))
            if event is not None and predicate(event):
                return event
        self.fail("订阅未收到预期事件")

    def _rows(self):
        with self.sessions() as observer:
            return {table.name: sorted(
                [dict(row) for row in observer.execute(select(table)).mappings()],
                key=lambda row: str(row.get("id", "")),
            ) for table in models.Base.metadata.sorted_tables}

    def test_start_without_subscriber_still_saves_real_kernel_chapter_and_cache_tombstone(self):
        node = self._nodes()[0]
        manager = self._manager()
        request_id = str(uuid.uuid4())
        with patch.object(auto_pilot.llm, "stream_complete", side_effect=self._gated_body) as provider:
            started = manager.start(self.novel_id, request_id)
            self.assertEqual(started["task_id"], request_id)
            self.assertTrue(self.blocked.wait(3))
            with self.sessions() as observer:
                active = AutopilotDraftRepo(observer).list(self.novel_id)
                self.assertEqual(active[0]["content"], "已经提交恢复缓存的第一段。")
                self.assertEqual(observer.query(models.Chapter).count(), 0)
            self.release.set()
            terminal = self._terminal(manager)
            provider.assert_called_once()
        self.assertEqual((terminal["task_id"], terminal["status"]), (request_id, "completed"))
        rows = self._rows()
        self.assertEqual(len(rows["chapter"]), 1)
        self.assertEqual(rows["chapter"][0]["outline_node_id"], node.id)
        self.assertEqual(rows["chapter"][0]["content"], "已经提交恢复缓存的第一段。后台独立完成的末段。")
        self.assertEqual(rows["chapter_revision"], [])
        self.assertEqual(rows["memory_chunk"], [])
        self.assertTrue(all(row["deleted"] and not row["content"] for row in rows["autopilot_draft"]))

    def test_closing_subscriber_after_first_text_keeps_worker_and_reconnect_uses_same_task(self):
        self._nodes()
        manager = self._manager()
        task_id = str(uuid.uuid4())
        with patch.object(auto_pilot.llm, "stream_complete", side_effect=self._gated_body) as provider:
            manager.start(self.novel_id, task_id)
            self.assertTrue(self.blocked.wait(3))
            first = self._subscribe(manager, task_id, after=0)
            delivered = self._until(first, lambda event: event["type"] == "text")
            first.close()
            still_running = manager.status(self.novel_id)
            self.assertTrue(still_running["running"])
            self.assertEqual(still_running["task_id"], task_id)
            self.assertTrue(self.registry.is_running(self.novel_id))
            resumed = self._subscribe(manager, task_id, after=delivered["event_id"])
            self.release.set()
            received = [event for frame in resumed if (event := _event(frame)) is not None]
            self.assertEqual([event["content"] for event in received if event["type"] == "text"],
                             ["后台独立完成的末段。"])
            self.assertTrue(all(event["event_id"] > delivered["event_id"] for event in received))
            self.assertEqual(sum(event["type"] == "done" for event in received), 1)
            provider.assert_called_once()
            self.assertEqual(self._terminal(manager)["status"], "completed")
        self.assertEqual(len(self._rows()["chapter"]), 1)

    def test_repeat_request_id_is_idempotent_while_different_id_conflicts_with_active_owner(self):
        self._nodes()
        manager = self._manager()
        task_id = str(uuid.uuid4())
        with patch.object(auto_pilot.llm, "stream_complete", side_effect=self._gated_body) as provider:
            manager.start(self.novel_id, task_id)
            self.assertTrue(self.blocked.wait(3))
            again = manager.start(self.novel_id, task_id)
            self.assertEqual(again["task_id"], task_id)
            with self.assertRaises(autopilot_tasks.TaskStorageError) as busy:
                manager.start(self.novel_id, str(uuid.uuid4()))
            self.assertEqual((busy.exception.status_code, busy.exception.task_id), (409, task_id))
            self.release.set()
            self._terminal(manager)
            completed_again = manager.start(self.novel_id, task_id)
            self.assertFalse(completed_again["running"])
            self.assertEqual(completed_again["task_id"], task_id)
            provider.assert_called_once()

    def test_two_simultaneous_starts_create_only_one_worker_and_model_call(self):
        self._nodes()
        manager = self._manager()
        barrier = threading.Barrier(3)
        results, errors = [], []

        def start():
            barrier.wait(timeout=3)
            try:
                results.append(manager.start(self.novel_id, str(uuid.uuid4())))
            except Exception as exc:
                errors.append(exc)

        with patch.object(auto_pilot.llm, "stream_complete", side_effect=self._gated_body) as provider:
            threads = [threading.Thread(target=start) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait(timeout=3)
            for thread in threads:
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
            self.assertEqual(len(results), 1)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], autopilot_tasks.TaskStorageError)
            self.assertEqual(errors[0].status_code, 409)
            self.assertTrue(self.blocked.wait(3))
            self.release.set()
            self._terminal(manager)
            provider.assert_called_once()
        self.assertEqual(len(self._rows()["chapter"]), 1)

    def test_two_subscribers_receive_independent_identical_ordered_history(self):
        self._nodes()
        manager = self._manager()
        task_id = str(uuid.uuid4())
        with patch.object(auto_pilot.llm, "stream_complete", return_value=iter(["第一段", "第二段"])) as provider:
            manager.start(self.novel_id, task_id)
            self._terminal(manager)
            first = [_event(frame) for frame in self._subscribe(manager, task_id, after=0)]
            second = [_event(frame) for frame in self._subscribe(manager, task_id, after=0)]
            first = [event for event in first if event is not None]
            second = [event for event in second if event is not None]
            self.assertEqual(first, second)
            ids = [event["event_id"] for event in first]
            self.assertEqual(ids, sorted(set(ids)))
            self.assertEqual(sum(event["type"] == "done" for event in first), 1)
            provider.assert_called_once()

    def test_fresh_reconnect_snapshot_contains_full_received_text_and_no_replayed_prefix(self):
        self._nodes()
        manager = self._manager()
        task_id = str(uuid.uuid4())
        with patch.object(auto_pilot.llm, "stream_complete", side_effect=self._gated_body):
            manager.start(self.novel_id, task_id)
            self.assertTrue(self.blocked.wait(3))
            subscriber = self._subscribe(manager, task_id)
            snapshot = _event(next(subscriber))
            self.assertEqual((snapshot["type"], snapshot["task_id"]), ("snapshot", task_id))
            self.assertEqual(snapshot["active_drafts"][0]["content"], "已经提交恢复缓存的第一段。")
            self.release.set()
            subsequent = [event for frame in subscriber if (event := _event(frame)) is not None]
            self.assertEqual([event["content"] for event in subsequent if event["type"] == "text"],
                             ["后台独立完成的末段。"])
            self.assertTrue(all(event["event_id"] > snapshot["event_id"] for event in subsequent))

    def test_terminal_cursor_reconnect_receives_saved_done_instead_of_bare_eof(self):
        self._nodes()
        manager = self._manager()
        task_id = str(uuid.uuid4())
        with patch.object(auto_pilot.llm, "stream_complete", return_value=iter(["完整正文"])) as provider:
            manager.start(self.novel_id, task_id)
            terminal = self._terminal(manager)
            received = [event for frame in self._subscribe(manager, task_id, after=terminal["event_id"])
                        if (event := _event(frame)) is not None]
            self.assertEqual(len(received), 1)
            self.assertEqual((received[0]["type"], received[0]["task_id"]), ("done", task_id))
            self.assertEqual(received[0]["event_id"], terminal["event_id"])
            provider.assert_called_once()

    def test_invalid_cursors_cross_novel_task_and_missing_novel_never_start_model(self):
        self._nodes()
        manager = self._manager()
        task_id = str(uuid.uuid4())
        with patch.object(auto_pilot.llm, "stream_complete", side_effect=self._gated_body) as provider:
            manager.start(self.novel_id, task_id)
            self.assertTrue(self.blocked.wait(3))
            for after in (-1, manager.status(self.novel_id)["event_id"] + 1000):
                with self.assertRaises(autopilot_tasks.TaskStorageError) as invalid:
                    list(manager.subscribe(self.novel_id, task_id, after=after))
                self.assertIn(invalid.exception.status_code, (409, 422))
            with self.assertRaises(autopilot_tasks.TaskStorageError) as cross:
                manager.get(self.other_id, task_id)
            self.assertEqual(cross.exception.status_code, 404)
            with self.assertRaises(autopilot_tasks.TaskStorageError) as missing:
                manager.start(str(uuid.uuid4()), str(uuid.uuid4()))
            self.assertEqual(missing.exception.status_code, 404)
            self.release.set()
            self._terminal(manager)
            provider.assert_called_once()

    def test_pause_at_chapter_boundary_resume_and_injection_work_without_subscriber(self):
        self._nodes(2)
        manager = self._manager()
        task_id = str(uuid.uuid4())
        calls = 0

        def chapters(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                yield from self._gated_body()
            else:
                yield "注入后的第二章完整正文。"

        with patch.object(auto_pilot.llm, "stream_complete", side_effect=chapters) as provider:
            manager.start(self.novel_id, task_id)
            self.assertTrue(self.blocked.wait(3))
            pause = manager.pause(self.novel_id, task_id)
            self.assertEqual(pause["task_id"], task_id)
            self.assertTrue(manager.status(self.novel_id)["pause_requested"])
            self.release.set()
            paused = self._wait(lambda: (status if (status := manager.status(self.novel_id))["paused"] else None),
                                "章间暂停")
            self.assertTrue(paused["running"])
            self.assertEqual(calls, 1)
            self.assertEqual(len(self._rows()["chapter"]), 1)
            directive = "下一章加入作者指定的会面。"
            manager.inject(self.novel_id, task_id, directive)
            resumed = manager.resume(self.novel_id, task_id)
            self.assertEqual(resumed["task_id"], task_id)
            terminal = self._terminal(manager)
            self.assertEqual(terminal["status"], "completed")
            self.assertEqual(provider.call_count, 2)
            self.assertIn(directive, auto_pilot.build_context.call_args.args)
        self.assertEqual(len(self._rows()["chapter"]), 2)

    def test_same_request_id_concurrent_retries_are_atomic_and_return_one_task(self):
        self._nodes()
        manager = self._manager()
        task_id = str(uuid.uuid4())
        barrier = threading.Barrier(3)
        results, errors = [], []

        def start():
            barrier.wait(timeout=3)
            try:
                results.append(manager.start(self.novel_id, task_id))
            except Exception as exc:
                errors.append(exc)

        with patch.object(auto_pilot.llm, "stream_complete", side_effect=self._gated_body) as provider:
            threads = [threading.Thread(target=start) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait(timeout=3)
            for thread in threads:
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual([result["task_id"] for result in results], [task_id, task_id])
            self.assertTrue(self.blocked.wait(3))
            self.release.set()
            self._terminal(manager)
            provider.assert_called_once()
        self.assertEqual(len(self._rows()["chapter"]), 1)

    def test_subscriber_read_does_not_rewrite_persisted_task_event_or_business_rows(self):
        self._nodes()
        manager = self._manager()
        task_id = str(uuid.uuid4())
        with patch.object(auto_pilot.llm, "stream_complete", return_value=iter(["完整正文。"])):
            manager.start(self.novel_id, task_id)
            terminal = self._terminal(manager)
        before = self._rows()
        task = next(row for row in before["autopilot_task"] if row["id"] == task_id)
        events = sorted([row for row in before["autopilot_task_event"] if row["task_id"] == task_id],
                        key=lambda row: row["event_id"])
        self.assertEqual(task["last_event_id"], terminal["event_id"])
        self.assertEqual(task["snapshot_json"]["event_id"], terminal["event_id"])
        self.assertEqual([row["event_id"] for row in events], list(range(1, terminal["event_id"] + 1)))
        self.assertTrue(all(row["data_json"]["task_id"] == task_id for row in events))
        for after in (None, 0, terminal["event_id"]):
            received = [event for frame in self._subscribe(manager, task_id, after=after)
                        if (event := _event(frame)) is not None]
            self.assertEqual(received[-1]["type"], "done")
        self.assertEqual(self._rows(), before)

    def test_worker_exception_is_terminal_persisted_and_cannot_leave_registry_busy(self):
        def failed_runner(novel_id, **kwargs):
            yield auto_pilot._sse({"type": "progress", "phase": "starting", "done": 0, "total": 1})
            raise RuntimeError("受控后台工作线程异常")

        manager = self._manager(runner=failed_runner)
        task_id = str(uuid.uuid4())
        manager.start(self.novel_id, task_id)
        terminal = self._terminal(manager)
        self.assertFalse(self.registry.is_running(self.novel_id))
        self.assertNotEqual(terminal["status"], "completed")
        events = [event for frame in self._subscribe(manager, task_id, after=0)
                  if (event := _event(frame)) is not None]
        self.assertTrue(any(event["type"] == "error" and event.get("fatal") for event in events))
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(sum(event["type"] == "done" for event in events), 1)
        self.assertEqual(self._rows()["chapter"], [])

    def test_old_task_replay_and_controls_do_not_change_new_active_task(self):
        self._nodes()
        manager = self._manager()
        first_id = str(uuid.uuid4())
        second_id = str(uuid.uuid4())
        with patch.object(auto_pilot.llm, "stream_complete", return_value=iter(["前次正文。"])):
            manager.start(self.novel_id, first_id)
            self._terminal(manager)
        # 新增独立缺失大纲章，用真实内核保持第二次运行在门闩处。
        repos.OutlineRepo(self.db).create(self.novel_id, level="chapter", title="后续章", sort_order=2)
        with patch.object(auto_pilot.llm, "stream_complete", side_effect=self._gated_body) as provider:
            manager.start(self.novel_id, second_id)
            self.assertTrue(self.blocked.wait(3))
            owner = self.registry.get(self.novel_id)
            for control in (manager.pause, manager.resume):
                with self.assertRaises(autopilot_tasks.TaskStorageError) as stale:
                    control(self.novel_id, first_id)
                self.assertIn(stale.exception.status_code, (409, 404))
            with self.assertRaises(autopilot_tasks.TaskStorageError):
                manager.inject(self.novel_id, first_id, "迟到指令不能污染后续任务")
            old = [event for frame in self._subscribe(manager, first_id, after=0)
                   if (event := _event(frame)) is not None]
            self.assertEqual(old[-1]["type"], "done")
            self.assertEqual(manager.status(self.novel_id)["task_id"], second_id)
            self.registry.finish(self.novel_id, owner_id=first_id)
            self.assertIs(self.registry.get(self.novel_id), owner)
            self.assertFalse(owner.paused)
            self.assertEqual(owner.injected_directive, "")
            self.release.set()
            self._terminal(manager)
            provider.assert_called_once()

    def test_process_restart_marks_orphan_task_interrupted_and_keeps_text_without_generating(self):
        node = self._nodes()[0]
        task_id, draft_id = str(uuid.uuid4()), str(uuid.uuid4())
        with self.sessions() as writer:
            repo = AutopilotTaskRepo(writer)
            repo.create(self.novel_id, task_id)
            AutopilotDraftRepo(writer).upsert(
                self.novel_id, draft_id, node_id=node.id, title=node.title, attempt=1,
                content="进程退出之前已经保存的半稿。", state="active", reason="", revision=2,
            )
            repo.append(self.novel_id, task_id, {
                "type": "progress", "phase": "generating", "node_id": node.id, "title": node.title,
                "generated": 0, "skipped": 0, "failed": 0, "done": 0, "total": 1,
                "draft_id": draft_id, "draft_revision": 2, "draft_attempt": 1, "draft_persisted": True,
            })
            repo.append(self.novel_id, task_id, {
                "type": "text", "node_id": node.id, "title": node.title,
                "content": "进程退出之前已经保存的半稿。", "draft_id": draft_id,
                "draft_revision": 2, "draft_attempt": 1, "draft_persisted": True,
            })
        before = self._rows()
        restarted_registry = auto_pilot.AutoPilotRegistry()
        restarted = self._manager(registry=restarted_registry)
        with patch.object(auto_pilot.llm, "stream_complete") as provider:
            self.assertEqual(restarted.recover_interrupted(), 1)
            status = restarted.status(self.novel_id)
            self.assertEqual((status["task_id"], status["status"], status["running"]),
                             (task_id, "interrupted", False))
            self.assertEqual(status["terminal_reason"], "backend_restarted")
            self.assertEqual(status["active_drafts"], [])
            self.assertFalse(restarted_registry.is_running(self.novel_id))
            events = [event for frame in self._subscribe(restarted, task_id, after=0)
                      if (event := _event(frame)) is not None]
            self.assertEqual(events[-1]["type"], "done")
            self.assertEqual(events[-1]["status"], "interrupted")
            self.assertTrue(any(event.get("code") == "backend_restarted" for event in events))
            historical = restarted.start(self.novel_id, task_id)
            self.assertEqual(historical["status"], "interrupted")
            after_recovery = self._rows()
            self.assertEqual(restarted.recover_interrupted(), 0)
            self.assertEqual(self._rows(), after_recovery)
            provider.assert_not_called()
        cache = after_recovery["autopilot_draft"][0]
        self.assertEqual((cache["content"], cache["state"], cache["revision"]),
                         ("进程退出之前已经保存的半稿。", "unconfirmed", 3))
        for table in before:
            if table not in {"autopilot_task", "autopilot_task_event", "autopilot_draft"}:
                self.assertEqual(after_recovery[table], before[table], table)

    def test_quality_and_drift_warnings_survive_snapshot_reconnect_with_successful_body(self):
        self._nodes()
        manager = self._manager()
        task_id = str(uuid.uuid4())
        with (
            patch.object(auto_pilot.llm, "stream_complete", return_value=iter(["确实已提交的完整正文。"])),
            patch.object(auto_pilot, "score_tension", side_effect=RuntimeError("评分服务不可用")),
            patch.object(auto_pilot, "check_drift", return_value=0.4),
        ):
            manager.start(self.novel_id, task_id)
            terminal = self._terminal(manager)
        self.assertEqual((terminal["status"], terminal["generated"], terminal["failed"]), ("completed", 1, 0))
        self.assertEqual(len(terminal["quality_warnings"]), 1)
        self.assertIn("评分", terminal["quality_warnings"][0])
        self.assertEqual(len(terminal["drift_warnings"]), 1)
        snapshot = _event(next(self._subscribe(manager, task_id)))
        self.assertEqual(snapshot["quality_warnings"], terminal["quality_warnings"])
        self.assertEqual(snapshot["drift_warnings"], terminal["drift_warnings"])
        self.assertEqual(self._rows()["chapter"][0]["content"], "确实已提交的完整正文。")

    def test_replay_crosses_multiple_database_batches_without_skipping_tail_events(self):
        node = self._nodes()[0]
        segments = [f"第{index}段。" for index in range(260)]

        def long_history(novel_id, **kwargs):
            yield {"type": "progress", "phase": "generating", "node_id": node.id, "title": node.title, "total": 1}
            for segment in segments:
                yield {"type": "text", "node_id": node.id, "title": node.title, "content": segment}
            yield {"type": "done", "status": "incomplete", "done": 0, "total": 1,
                   "failed": 1, "message": "事件历史验收结束，不创建正式正文"}

        manager = self._manager(runner=long_history)
        task_id = str(uuid.uuid4())
        manager.start(self.novel_id, task_id)
        terminal = self._terminal(manager)
        self.assertGreater(terminal["event_id"], 256)
        events = [event for frame in self._subscribe(manager, task_id, after=0)
                  if (event := _event(frame)) is not None]
        self.assertEqual([event["content"] for event in events if event["type"] == "text"], segments)
        self.assertEqual([event["event_id"] for event in events], list(range(1, terminal["event_id"] + 1)))
        self.assertEqual(sum(event["type"] == "done" for event in events), 1)
        self.assertEqual(self._rows()["chapter"], [])

    def test_event_storage_failure_rolls_back_cursor_and_preserves_partial_without_chapter(self):
        self._nodes()
        with self.engine.begin() as conn:
            conn.execute(text(
                "CREATE TRIGGER reject_text_event BEFORE INSERT ON autopilot_task_event "
                "WHEN json_extract(NEW.data_json, '$.type') = 'text' "
                "BEGIN SELECT RAISE(ABORT, 'public event storage failure'); END"
            ))
        manager = self._manager()
        task_id = str(uuid.uuid4())
        with patch.object(auto_pilot.llm, "stream_complete", side_effect=self._gated_body) as provider:
            manager.start(self.novel_id, task_id)
            terminal = self._terminal(manager)
            provider.assert_called_once()
        self.assertEqual(terminal["status"], "interrupted")
        self.assertFalse(self.registry.is_running(self.novel_id))
        rows = self._rows()
        self.assertEqual(rows["chapter"], [])
        self.assertEqual(rows["autopilot_draft"][0]["content"], "已经提交恢复缓存的第一段。")
        self.assertEqual(rows["autopilot_draft"][0]["state"], "unconfirmed")
        events = sorted(rows["autopilot_task_event"], key=lambda row: row["event_id"])
        self.assertFalse(any(row["data_json"]["type"] == "text" for row in events))
        self.assertEqual([row["event_id"] for row in events], list(range(1, terminal["event_id"] + 1)))
        self.assertEqual(rows["autopilot_task"][0]["last_event_id"], terminal["event_id"])
        self.assertEqual(rows["autopilot_task"][0]["snapshot_json"]["active_drafts"], [])

    def test_shutdown_keeps_received_partial_and_prevents_delayed_tail_from_saving_chapter(self):
        self._nodes()
        manager = self._manager()
        task_id = str(uuid.uuid4())
        closed = threading.Event()

        def interrupted_body(*args, **kwargs):
            try:
                yield "关闭后台之前已保存的片段。"
                self.blocked.set()
                if not self.release.wait(5):
                    raise RuntimeError("关闭测试门闩未释放")
                yield "关闭后台之后迟到的末段，不能写入章节。"
            finally:
                closed.set()

        with patch.object(auto_pilot.llm, "stream_complete", side_effect=interrupted_body) as provider:
            manager.start(self.novel_id, task_id)
            self.assertTrue(self.blocked.wait(3))
            manager.shutdown()
            stopped = manager.status(self.novel_id)
            self.assertEqual((stopped["status"], stopped["running"]), ("interrupted", False))
            self.assertEqual(stopped["terminal_reason"], "backend_shutdown")
            with self.assertRaises(autopilot_tasks.TaskStorageError) as closing:
                manager.start(self.novel_id, str(uuid.uuid4()))
            self.assertEqual(closing.exception.status_code, 503)
            self.release.set()
            self.assertTrue(manager.wait(task_id, timeout=3))
            self.assertTrue(closed.is_set())
            provider.assert_called_once()
        self.assertFalse(self.registry.is_running(self.novel_id))
        rows = self._rows()
        self.assertEqual(rows["chapter"], [])
        self.assertEqual(rows["autopilot_draft"][0]["content"], "关闭后台之前已保存的片段。")
        self.assertEqual(rows["autopilot_draft"][0]["state"], "unconfirmed")


if __name__ == "__main__":
    unittest.main()
