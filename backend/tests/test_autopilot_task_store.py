"""真实 SQLite 文件验收任务身份、原子投影、游标和串行事件写入。"""
from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
import uuid
from pathlib import Path

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.infrastructure import models
from app.infrastructure.autopilot_tasks import AutopilotTaskRepo, TaskStorageError
from app.infrastructure import repository as repos
from app.interfaces.schemas import AutopilotTaskSnapshot


class AutopilotTaskStoreTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database = (Path(directory.name) / "task-store.db").as_posix()
        self.engine = create_engine(f"sqlite:///{database}", connect_args={"check_same_thread": False})
        models.Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.sessions()
        self.addCleanup(self.db.close)
        self.novel_id = repos.NovelRepo(self.db).create(title="事件恢复作品").id
        self.other_id = repos.NovelRepo(self.db).create(title="另一作品").id
        self.task_id = str(uuid.uuid4())
        self.draft_id = str(uuid.uuid4())
        self.node_id = str(uuid.uuid4())
        self.repo = AutopilotTaskRepo(self.db)

    def _rows(self):
        with self.sessions() as observer:
            return {
                table.name: sorted([dict(row) for row in observer.execute(select(table)).mappings()],
                                   key=lambda row: row["id"])
                for table in models.Base.metadata.sorted_tables
            }

    def _start(self):
        return self.repo.create(self.novel_id, self.task_id)

    def _append(self, **event):
        return self.repo.append(self.novel_id, self.task_id, event)

    def _generating(self):
        return self._append(type="progress", phase="generating", node_id=self.node_id, title="当前章",
                            draft_id=self.draft_id, draft_revision=1, draft_attempt=2,
                            draft_persisted=True, total=3, message="开始当前章")

    def test_request_id_is_canonical_and_idempotent_even_after_terminal_state(self):
        initial, created = self._start()
        self.assertTrue(created)
        same, created = self.repo.create(self.novel_id, self.task_id.upper())
        self.assertFalse(created)
        self.assertEqual(same, initial)
        self._append(type="done", status="completed", generated=1, skipped=0, failed=0, done=1, total=1)
        terminal = self.repo.get(self.novel_id, self.task_id)
        same, created = self._start()
        self.assertFalse(created)
        self.assertEqual(same, terminal)
        self.assertEqual(len(self._rows()["autopilot_task"]), 1)

    def test_different_request_id_cannot_start_second_active_task_and_names_owner(self):
        self._start()
        with self.assertRaises(TaskStorageError) as caught:
            self.repo.create(self.novel_id, str(uuid.uuid4()))
        self.assertEqual((caught.exception.status_code, caught.exception.code, caught.exception.task_id),
                         (409, "autopilot_busy", self.task_id))
        self.assertEqual(len(self._rows()["autopilot_task"]), 1)

    def test_every_operation_keeps_novel_scope_and_missing_novel_has_no_side_effect(self):
        self._start()
        for action in (
            lambda: self.repo.get(self.other_id, self.task_id),
            lambda: self.repo.append(self.other_id, self.task_id, {"type": "done"}),
            lambda: self.repo.create(self.other_id, self.task_id),
            lambda: self.repo.read_batch(self.other_id, self.task_id, 0),
        ):
            with self.assertRaises(TaskStorageError) as caught:
                action()
            self.assertEqual(caught.exception.status_code, 404)
        before = self._rows()
        missing = str(uuid.uuid4())
        with self.assertRaises(TaskStorageError):
            self.repo.create(missing, str(uuid.uuid4()))
        self.assertEqual(self._rows(), before)
        self.assertIsNone(self.repo.latest(self.other_id))

    def test_snapshot_contains_only_published_content_even_when_raw_cache_is_ahead(self):
        self._start()
        self._generating()
        self._append(type="text", node_id=self.node_id, content="已经公开。", draft_id=self.draft_id,
                     draft_revision=2, draft_attempt=2, draft_persisted=True)
        self.db.add(models.AutopilotDraft(
            id=self.draft_id, novel_id=self.novel_id, node_id=self.node_id,
            title="当前章", attempt=2, content="已经公开。缓存先保存但还没公开的新段落。", revision=3,
        ))
        self.db.commit()
        with self.sessions() as observer:
            snapshot = AutopilotTaskRepo(observer).get(self.novel_id, self.task_id)
        self.assertEqual(snapshot["event_id"], 2)
        self.assertEqual(snapshot["active_drafts"][0]["content"], "已经公开。")
        self.assertEqual(snapshot["active_drafts"][0]["draft_revision"], 2)
        AutopilotTaskSnapshot.model_validate(snapshot)

    def test_failed_event_insert_rolls_back_snapshot_and_cursor_without_a_gap(self):
        self._start()
        self._generating()
        before = self._rows()
        with self.engine.begin() as connection:
            connection.execute(text(
                "CREATE TRIGGER reject_event_insert BEFORE INSERT ON autopilot_task_event "
                "WHEN NEW.event_id = 2 BEGIN SELECT RAISE(ABORT, 'event write failure'); END"
            ))
        with self.assertRaises(Exception):
            self._append(type="text", node_id=self.node_id, draft_id=self.draft_id, content="不能公开的片段")
        self.assertEqual(self._rows(), before)
        with self.engine.begin() as connection:
            connection.execute(text("DROP TRIGGER reject_event_insert"))
        published = self._append(type="text", node_id=self.node_id, draft_id=self.draft_id, content="正常片段")
        self.assertEqual(published["event_id"], 2)
        self.assertEqual(self.repo.get(self.novel_id, self.task_id)["active_drafts"][0]["content"], "正常片段")

    def test_controls_quality_warnings_and_terminal_snapshot_preserve_accurate_counts(self):
        self._start()
        self._generating()
        self._append(type="control", action="pause", paused=False, pause_requested=True, message="请求暂停")
        requested = self.repo.get(self.novel_id, self.task_id)
        self.assertFalse(requested["paused"])
        self.assertTrue(requested["pause_requested"])
        self._append(type="paused", message="已暂停")
        paused = self.repo.get(self.novel_id, self.task_id)
        self.assertEqual((paused["status"], paused["paused"], paused["pause_requested"]), ("paused", True, False))
        self._append(type="control", action="resume", paused=False, pause_requested=False)
        self._append(type="error", scope="quality", saved=True, fatal=False, message="正文已保存，评分失败")
        self._append(type="drift", seq=2, score=0.3, warning=True)
        terminal_event = self._append(type="done", status="incomplete", reason="failure_threshold",
                                      generated=1, skipped=1, failed=1, done=2, total=3, message="任务结束")
        terminal = self.repo.get(self.novel_id, self.task_id)
        self.assertEqual((terminal["running"], terminal["paused"], terminal["pause_requested"]), (False, False, False))
        self.assertEqual(terminal["active_drafts"], [])
        self.assertEqual(terminal["quality_warnings"], ["正文已保存，评分失败"])
        self.assertEqual(terminal["drift_warnings"][0]["score"], 0.3)
        self.assertEqual((terminal["generated"], terminal["skipped"], terminal["failed"], terminal["done"]), (1, 1, 1, 2))
        self.assertEqual(self.repo.terminal_event(self.novel_id, self.task_id), terminal_event)
        AutopilotTaskSnapshot.model_validate(terminal)

    def test_failed_chapter_removes_only_its_active_projection_and_never_creates_failed_fact(self):
        self._start()
        self._generating()
        self._append(type="text", node_id=self.node_id, draft_id=self.draft_id, content="已收到的半稿")
        self._append(type="error", scope="chapter", saved=False, node_id=self.node_id,
                     draft_id=self.draft_id, failed=1, message="本章未保存")
        self.assertEqual(self.repo.get(self.novel_id, self.task_id)["active_drafts"], [])
        self.assertEqual(self._rows()["chapter"], [])
        self.assertEqual(self._rows()["chapter_revision"], [])

    def test_batches_are_cursor_ordered_and_bad_cursors_are_rejected_without_mutation(self):
        self._start()
        for index in range(1, 6):
            self._append(type="progress", phase="starting", message=f"事件{index}")
        snapshot, batch = self.repo.read_batch(self.novel_id, self.task_id, 1, limit=2)
        self.assertEqual(snapshot["event_id"], 5)
        self.assertEqual([item["event_id"] for item in batch], [2, 3])
        before = self._rows()
        for cursor in (-1, 6, True, 1.5):
            with self.assertRaises(TaskStorageError) as caught:
                self.repo.read_batch(self.novel_id, self.task_id, cursor)
            self.assertEqual(caught.exception.code, "task_cursor_invalid")
        self.assertEqual(self._rows(), before)

    def test_parallel_control_publishers_are_serialized_and_no_event_is_lost(self):
        self._start()

        def publish(writer_number):
            with self.sessions() as writer:
                repo = AutopilotTaskRepo(writer)
                return [repo.append(self.novel_id, self.task_id, {
                    "type": "error", "scope": "quality", "saved": True,
                    "message": f"写者{writer_number}的事件{index}",
                })["event_id"] for index in range(8)]

        with ThreadPoolExecutor(max_workers=4) as workers:
            sequences = list(workers.map(publish, range(4)))
        self.assertEqual(sorted(item for sequence in sequences for item in sequence), list(range(1, 33)))
        snapshot = self.repo.get(self.novel_id, self.task_id)
        self.assertEqual(snapshot["event_id"], 32)
        self.assertEqual(len(snapshot["quality_warnings"]), 32)
        self.assertEqual(len(self._rows()["autopilot_task_event"]), 32)

    def test_invalid_events_roll_back_and_terminal_tasks_cannot_accept_new_output(self):
        self._start()
        before = self._rows()
        for event in ({"type": "text", "node_id": self.node_id, "content": 3},
                      {"type": "unknown"}, {"type": "done", "status": "running"}):
            with self.assertRaises(TaskStorageError):
                self.repo.append(self.novel_id, self.task_id, event)
            self.assertEqual(self._rows(), before)
        self._append(type="done", status="completed")
        with self.assertRaises(TaskStorageError) as caught:
            self._append(type="progress", message="迟到事件")
        self.assertEqual(caught.exception.code, "task_finished")

    def test_task_lifecycle_changes_no_novel_chapter_story_bible_or_cache_records(self):
        repos.ChapterRepo(self.db).create(self.novel_id, title="保护章节", content="完整作者稿", status="finalized")
        repos.CharacterRepo(self.db).create(self.novel_id, name="保护人物", known_info="已有设定")
        before = self._rows()
        self._start()
        self._generating()
        self._append(type="text", node_id=self.node_id, draft_id=self.draft_id, content="任务日志片段")
        self._append(type="done", status="interrupted", reason="backend_restarted")
        after = self._rows()
        for name in before:
            if name not in ("autopilot_task", "autopilot_task_event"):
                self.assertEqual(after[name], before[name], name)

    def test_delete_novel_cascades_tasks_and_events_only_for_that_novel(self):
        self._start()
        self._append(type="done", status="completed")
        other_task = str(uuid.uuid4())
        self.repo.create(self.other_id, other_task)
        self.repo.append(self.other_id, other_task, {"type": "done", "status": "completed"})
        repos.NovelRepo(self.db).delete(self.novel_id)
        rows = self._rows()
        self.assertEqual([item["novel_id"] for item in rows["autopilot_task"]], [self.other_id])
        self.assertEqual([item["novel_id"] for item in rows["autopilot_task_event"]], [self.other_id])

    def test_recovered_sqlite_writes_can_finish_failed_identity_and_start_new_retry(self):
        from app.application import auto_pilot, autopilot_tasks

        calls = []

        def controlled_runner(novel_id, session_factory, reserved_state):
            calls.append(novel_id)
            yield {"type": "progress", "phase": "starting", "message": "后台已启动"}
            yield {"type": "done", "status": "completed", "message": "已完成"}

        manager = autopilot_tasks.AutopilotTaskManager(
            session_factory=self.sessions, registry=auto_pilot.AutoPilotRegistry(), runner=controlled_runner,
            heartbeat_seconds=0.01,
        )
        self.addCleanup(manager.shutdown)
        with self.engine.begin() as connection:
            connection.execute(text(
                "CREATE TRIGGER reject_all_task_events BEFORE INSERT ON autopilot_task_event "
                "BEGIN SELECT RAISE(ABORT, 'temporary task storage failure'); END"
            ))
        manager.start(self.novel_id, self.task_id)
        self.assertTrue(manager.wait(self.task_id))
        self.assertEqual(manager.get(self.novel_id, self.task_id)["status"], "interrupted")
        self.assertEqual(self.repo.get(self.novel_id, self.task_id)["status"], "starting")
        new_request = str(uuid.uuid4())
        with self.assertRaises(TaskStorageError) as caught:
            manager.start(self.novel_id, new_request)
        self.assertEqual((caught.exception.status_code, caught.exception.code), (503, "task_storage_unavailable"))
        self.assertEqual(calls, [self.novel_id])
        with self.engine.begin() as connection:
            connection.execute(text("DROP TRIGGER reject_all_task_events"))
        started = manager.start(self.novel_id, new_request)
        self.assertEqual(started["task_id"], new_request)
        self.assertTrue(manager.wait(new_request))
        self.assertEqual(self.repo.get(self.novel_id, self.task_id)["status"], "interrupted")
        self.assertEqual(manager.get(self.novel_id, new_request)["status"], "completed")
        self.assertEqual(calls, [self.novel_id, self.novel_id])
        # 即使旧请求的首次响应丢失，重发旧 ID 也不能变成第三次生成。
        self.assertEqual(manager.start(self.novel_id, self.task_id)["status"], "interrupted")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
