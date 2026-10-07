"""实际HTTP、独立SQLite连接和内核验证：构思回执仅确认持久记录。"""
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from app.application import auto_pilot, autopilot_tasks
from app.infrastructure import models, repository as repos
from app.infrastructure import autopilot_tasks as task_storage
from app.infrastructure.autopilot_tasks import AutopilotTaskRepo
from app.infrastructure.db import get_db
from app.interfaces.api.v1.autopilot import router
from app.interfaces.api.v1.novels import router as novels_router
from app.interfaces.schemas import StoryCheckResult


class AutopilotInjectionReceiptTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "injection-receipts.db"
        self.engine = create_engine(f"sqlite:///{self.path.as_posix()}",
                                   connect_args={"check_same_thread": False, "timeout": .1})
        self.addCleanup(self.engine.dispose)
        models.Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        self.novel_id, self.other_id = self._novel("原构思作品"), self._novel("另一作品")
        self.task_id, self.other_task = self._task(self.novel_id), self._task(self.other_id)
        self.registry = auto_pilot.AutoPilotRegistry()
        self.registry.start(self.novel_id, owner_id=self.task_id)
        self.registry.start(self.other_id, owner_id=self.other_task)
        self.manager = autopilot_tasks.AutopilotTaskManager(session_factory=self.sessions, registry=self.registry)
        for mocked in (patch.object(autopilot_tasks, "manager", self.manager),
                       patch.object(auto_pilot, "registry", self.registry)):
            mocked.start()
            self.addCleanup(mocked.stop)
        self.app = FastAPI()
        self.app.include_router(router, prefix="/api/v1")
        self.app.include_router(novels_router, prefix="/api/v1")

        def database():
            with self.sessions() as db:
                yield db
        self.app.dependency_overrides[get_db] = database
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.addCleanup(self.manager.shutdown)
        self.base = f"/api/v1/novels/{self.novel_id}/autopilot/tasks/{self.task_id}"

    def _novel(self, title):
        with self.sessions() as db:
            return repos.NovelRepo(db).create(title=title).id

    def _task(self, novel_id):
        task_id = str(uuid.uuid4())
        with self.sessions() as db:
            AutopilotTaskRepo(db).create(novel_id, task_id)
        return task_id

    def _send(self, key, directive="原构思", base=None):
        return self.client.post((base or self.base) + "/inject",
                                json={"request_id": key, "directive": directive})

    def _get(self, key, base=None):
        return self.client.get((base or self.base) + f"/injections/{key}")

    def _rows(self):
        with self.sessions() as db:
            return {table.name: [dict(row) for row in db.execute(select(table)).mappings()]
                    for table in models.Base.metadata.sorted_tables}

    def _finish(self, novel_id=None, task_id=None):
        novel_id, task_id = novel_id or self.novel_id, task_id or self.task_id
        with self.sessions() as db:
            AutopilotTaskRepo(db).interrupt(novel_id, task_id, "测试任务结束")
        self.registry.finish(novel_id, owner_id=task_id)

    def test_exact_unicode_payload_canonical_uuid_and_read_only_original_ack(self):
        key = str(uuid.uuid4())
        directive = " \t原构思\u3000😀\x00\n"
        response = self._send(key.upper().replace("-", ""), directive)
        self.assertEqual(response.status_code, 200, response.text)
        ack = response.json()
        self.assertEqual((ack["type"], ack["action"], ack["novel_id"], ack["task_id"], ack["request_id"]),
                         ("control", "inject", self.novel_id, self.task_id, key))
        self.assertEqual(ack["directive"], directive)
        self.assertEqual(ack["directive_hash"], hashlib.sha256(directive.encode("utf-8")).hexdigest())
        self.assertIs(ack["accepted"], True)
        self.assertIs(ack["receipt_replayed"], False)
        self.assertEqual(ack["message"], "构思已记录，请核对实际章节执行结果")
        self.assertEqual(self.registry.get(self.novel_id).injected_directive, directive)
        before = self._rows()
        receipt = self._get(key)
        self.assertEqual(receipt.status_code, 200)
        self.assertEqual(receipt.json(), {**ack, "receipt_replayed": True})
        self.assertEqual(self._rows(), before)
        self.assertEqual(before["autopilot_task_event"][0]["data_json"], ack)
        self.assertFalse(any(field in ack for field in ("applied", "saved", "queued", "chapter_id")))

    def test_legacy_directive_only_and_legacy_address_remain_compatible(self):
        response = self.client.post(self.base + "/inject", json={"directive": "旧客户端"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("request_id", response.json())
        self.assertNotIn("accepted", response.json())
        key = str(uuid.uuid4())
        legacy = f"/api/v1/novels/{self.novel_id}/autopilot/inject?task_id={self.task_id}"
        response = self.client.post(legacy, json={"directive": "新请求旧地址", "request_id": key})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._get(key).json()["directive"], "新请求旧地址")

    def test_invalid_input_and_unknown_key_do_not_fabricate_acceptance(self):
        before = self._rows()
        missing = self._get(str(uuid.uuid4()))
        self.assertEqual((missing.status_code, missing.json()["code"]), (404, "autopilot_injection_not_found"))
        self.assertEqual(self._get("invalid").status_code, 422)
        for key, directive in (("not-uuid", "有效构思"), (True, "有效构思"),
                               (str(uuid.uuid4()), " \u3000\t"), (str(uuid.uuid4()), None)):
            with self.subTest(key=key, directive=directive):
                self.assertEqual(self._send(key, directive).status_code, 422)
        self.assertEqual(self._rows(), before)
        self.assertEqual(self.registry.get(self.novel_id).injected_directive, "")

    def test_same_nonce_payload_and_global_scope_conflicts_without_leaking_original(self):
        key = str(uuid.uuid4())
        self.assertEqual(self._send(key, "原文 ").status_code, 200)
        before = self._rows()
        for directive in ("原文", "原文  ", "另一构思"):
            response = self._send(key, directive)
            self.assertEqual((response.status_code, response.json()["code"]),
                             (409, "autopilot_injection_request_conflict"))
        foreign = f"/api/v1/novels/{self.other_id}/autopilot/tasks/{self.other_task}"
        response = self._send(key, "原文 ", foreign)
        self.assertEqual((response.status_code, response.json()["code"]),
                         (409, "autopilot_injection_request_conflict"))
        self.assertNotIn("directive", response.json())
        self.assertEqual(self._get(key, foreign).status_code, 404)
        wrong = f"/api/v1/novels/{self.other_id}/autopilot/tasks/{self.task_id}"
        self.assertEqual(self._send(key, "原文 ", wrong).status_code, 404)
        self.assertEqual(self._get(key, wrong).status_code, 404)
        self.assertEqual(self._rows(), before)
        self.assertEqual(self.registry.get(self.other_id).injected_directive, "")

    def test_replay_after_consumption_or_replacement_does_not_reinstall_old_directive(self):
        key = str(uuid.uuid4())
        ack = self._send(key, "构思A").json()
        self.registry.get(self.novel_id).injected_directive = ""  # 内核已取出。
        with patch.object(self.registry, "inject", wraps=self.registry.inject) as dispatch:
            replay = self._send(key, "构思A")
            self.assertEqual(replay.json(), {**ack, "receipt_replayed": True})
            dispatch.assert_not_called()
        self._send(str(uuid.uuid4()), "较新构思B")
        before = self._rows()
        with patch.object(self.registry, "inject", wraps=self.registry.inject) as dispatch:
            self.assertEqual(self._send(key, "构思A").status_code, 200)
            dispatch.assert_not_called()
        self.assertEqual(self.registry.get(self.novel_id).injected_directive, "较新构思B")
        self.assertEqual(self._rows(), before)

    def test_same_text_with_new_nonce_is_a_legal_new_author_operation(self):
        first = self._send(str(uuid.uuid4()), "相同原文").json()
        self.registry.get(self.novel_id).injected_directive = ""
        second = self._send(str(uuid.uuid4()), "相同原文").json()
        self.assertGreater(second["event_id"], first["event_id"])
        self.assertFalse(second["receipt_replayed"])
        self.assertEqual(self.registry.get(self.novel_id).injected_directive, "相同原文")

    def test_terminal_and_closing_history_replays_before_active_owner_rejection(self):
        key = str(uuid.uuid4())
        ack = self._send(key).json()
        self._finish()
        self.manager._closing = True
        before = self._rows()
        with patch.object(self.registry, "inject") as dispatch:
            self.assertEqual(self._send(key).json(), {**ack, "receipt_replayed": True})
            self.assertEqual(self._get(key).json(), {**ack, "receipt_replayed": True})
            dispatch.assert_not_called()
        self.assertEqual(self._send(str(uuid.uuid4())).status_code, 503)
        self.assertEqual(self._rows(), before)

    def test_restart_interrupts_without_restoring_generation_but_keeps_original_receipt(self):
        key = str(uuid.uuid4())
        ack = self._send(key, "重启前的原构思").json()
        fresh_registry = auto_pilot.AutoPilotRegistry()
        restarted = autopilot_tasks.AutopilotTaskManager(session_factory=self.sessions, registry=fresh_registry)
        self.addCleanup(restarted.shutdown)
        self.assertEqual(restarted.recover_interrupted(), 2)
        before = self._rows()
        with patch.object(autopilot_tasks, "manager", restarted), patch.object(fresh_registry, "inject") as dispatch:
            self.assertEqual(self._get(key).json(), {**ack, "receipt_replayed": True})
            self.assertEqual(self._send(key, "重启前的原构思").json(), {**ack, "receipt_replayed": True})
            dispatch.assert_not_called()
        self.assertEqual(self._rows(), before)
        self.assertFalse(fresh_registry.is_running(self.novel_id))
        self.assertEqual(restarted.status(self.novel_id)["status"], "interrupted")

    def test_real_event_trigger_rollback_keeps_snapshot_and_registry_unchanged(self):
        self.client.post(self.base + "/inject", json={"directive": "已有原构思"})
        before = self._rows()
        with self.engine.begin() as db:
            db.exec_driver_sql("""CREATE TRIGGER reject_receipt BEFORE INSERT ON autopilot_task_event
                WHEN json_extract(NEW.data_json, '$.request_id') IS NOT NULL
                BEGIN SELECT RAISE(ABORT, 'controlled receipt rejection'); END""")
        key = str(uuid.uuid4())
        self.assertEqual(self._send(key, "不能提交的新构思").status_code, 500)
        self.assertEqual(self._rows(), before)
        self.assertEqual(self.registry.get(self.novel_id).injected_directive, "已有原构思")
        self.assertEqual(self._get(key).status_code, 404)

    def test_real_postcommit_exclusive_lock_cannot_turn_saved_receipt_into_failure(self):
        key = str(uuid.uuid4())
        original_commit = self.sessions.class_.commit
        blockers, after_commit_sql = [], []
        committed = threading.Event()

        def lock_after_commit(db):
            original_commit(db)
            blocker = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=.1)
            blocker.execute("BEGIN EXCLUSIVE")
            blockers.append(blocker)
            committed.set()

        def sql(_connection, _cursor, statement, *_args):
            if committed.is_set():
                after_commit_sql.append(statement)

        event.listen(self.engine, "before_cursor_execute", sql)
        try:
            with patch.object(self.sessions.class_, "commit", new=lock_after_commit):
                response = self._send(key)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["accepted"])
            self.assertEqual(after_commit_sql, [])
        finally:
            for blocker in blockers:
                blocker.rollback()
                blocker.close()
            event.remove(self.engine, "before_cursor_execute", sql)
        self.assertEqual(self._get(key).status_code, 200)

    def test_two_independent_managers_and_sessions_accept_and_dispatch_same_nonce_once(self):
        separate_engine = create_engine(f"sqlite:///{self.path.as_posix()}",
                                       connect_args={"check_same_thread": False})
        self.addCleanup(separate_engine.dispose)
        registry2 = auto_pilot.AutoPilotRegistry()
        registry2.start(self.novel_id, owner_id=self.task_id)
        manager2 = autopilot_tasks.AutopilotTaskManager(
            session_factory=sessionmaker(bind=separate_engine), registry=registry2)
        self.addCleanup(manager2.shutdown)
        barrier, results, errors = threading.Barrier(3), [], []
        key = str(uuid.uuid4())

        def submit(manager):
            try:
                barrier.wait(timeout=3)
                results.append(manager.inject(self.novel_id, self.task_id, "同次并发原文", key))
            except Exception as error:
                errors.append(error)
        with patch.object(self.registry, "inject", wraps=self.registry.inject) as first, \
                patch.object(registry2, "inject", wraps=registry2.inject) as second:
            workers = [threading.Thread(target=submit, args=(manager,)) for manager in (self.manager, manager2)]
            for worker in workers:
                worker.start()
            barrier.wait(timeout=3)
            for worker in workers:
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(first.call_count + second.call_count, 1)
        self.assertEqual(sorted(row["receipt_replayed"] for row in results), [False, True])
        self.assertEqual(results[0]["event_id"], results[1]["event_id"])
        self.assertEqual(len(self._rows()["autopilot_task_event"]), 1)

    def test_database_writer_serialization_deduplicates_without_python_mutex(self):
        barrier, results, errors = threading.Barrier(3), [], []
        key = str(uuid.uuid4())

        def write():
            try:
                with self.sessions() as db:
                    barrier.wait(timeout=3)
                    results.append(AutopilotTaskRepo(db).accept_injection(self.novel_id, self.task_id,
                        key, "数据库并发原文", {"paused": False, "pause_requested": False}))
            except Exception as error:
                errors.append(error)
        # 去掉仅进程内互斥，确认真正SQLite BEGIN IMMEDIATE覆盖查重+游标+事件写入。
        with patch.object(task_storage, "draft_write_lock", nullcontext()):
            workers = [threading.Thread(target=write) for _ in range(2)]
            for worker in workers:
                worker.start()
            barrier.wait(timeout=3)
            for worker in workers:
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sorted(created for _, created in results), [False, True])
        self.assertEqual(len(self._rows()["autopilot_task_event"]), 1)

    def test_receipt_write_rejects_caller_pending_flushed_and_core_dml_without_rollback(self):
        for mode in ("pending", "flushed", "core"):
            with self.subTest(mode=mode), self.sessions() as db:
                if mode == "core":
                    db.execute(text("UPDATE novel SET title='未提交作者修改' WHERE id=:id"), {"id": self.novel_id})
                else:
                    db.add(models.Character(novel_id=self.novel_id, name="调用方未提交人物"))
                    if mode == "flushed":
                        db.flush()
                with patch.object(db, "commit", wraps=db.commit) as commit, \
                        patch.object(db, "rollback", wraps=db.rollback) as rollback:
                    with self.assertRaises(RuntimeError):
                        AutopilotTaskRepo(db).accept_injection(self.novel_id, self.task_id,
                            str(uuid.uuid4()), "不要提交作者事务", {"paused": False, "pause_requested": False})
                    commit.assert_not_called()
                    rollback.assert_not_called()
                self.assertTrue(db.in_transaction())
                db.rollback()
        self.assertEqual(self._rows()["character"], [])

    def test_history_read_and_replay_never_commit_another_sessions_chapter_transaction(self):
        key = str(uuid.uuid4())
        self._send(key)
        with self.sessions() as db:
            chapter = repos.ChapterRepo(db).create(self.novel_id, title="作者稿", content="真实原稿")
            chapter_id = chapter.id
            db.execute(text("UPDATE chapter SET content='作者未提交新稿' WHERE id=:id"), {"id": chapter_id})
            with patch.object(db, "commit", wraps=db.commit) as commit, \
                    patch.object(db, "rollback", wraps=db.rollback) as rollback:
                self.assertEqual(self._get(key).status_code, 200)
                self.assertEqual(self._send(key).status_code, 200)
                commit.assert_not_called()
                rollback.assert_not_called()
            db.rollback()
        with self.sessions() as observer:
            self.assertEqual(repos.ChapterRepo(observer).get(self.novel_id, chapter_id).content, "真实原稿")

    def test_registry_dispatch_failure_still_returns_truthful_durable_acceptance(self):
        key = str(uuid.uuid4())
        with patch.object(self.registry, "inject", side_effect=RuntimeError("受控派发失败")) as dispatch:
            response = self._send(key)
            self.assertEqual(response.status_code, 200)
            ack = response.json()
            self.assertTrue(ack["accepted"])
            self.assertEqual(self.registry.get(self.novel_id).injected_directive, "")
            self.assertEqual(self._get(key).json(), {**ack, "receipt_replayed": True})
            self.assertEqual(self._send(key).json(), {**ack, "receipt_replayed": True})
            dispatch.assert_called_once()
        self.assertNotIn("applied", ack)

    def test_sse_retains_original_event_and_receipt_query_does_not_advance_cursor(self):
        key = str(uuid.uuid4())
        ack = self._send(key).json()
        self._finish()
        before = self._rows()
        self._get(key)
        self._send(key)
        self.assertEqual(self._rows(), before)
        events = self.client.get(self.base + "/events?after=0")
        self.assertEqual(events.status_code, 200)
        values = [json.loads(line[5:].strip()) for line in events.text.splitlines() if line.startswith("data:")]
        control = next(row for row in values if row.get("request_id") == key)
        self.assertEqual(control, ack)
        self.assertFalse(control["receipt_replayed"])
        self.assertGreater(values[-1]["event_id"], ack["event_id"])

    def test_novel_delete_cleans_receipts_and_preserves_other_scope(self):
        key = str(uuid.uuid4())
        self._send(key)
        self._finish()
        before = [row for row in self._rows()["autopilot_task"] if row["novel_id"] == self.other_id]
        deleted = self.client.delete(f"/api/v1/novels/{self.novel_id}")
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(self._get(key).status_code, 404)
        rows = self._rows()
        self.assertFalse(any(row["novel_id"] == self.novel_id for row in rows["autopilot_task_event"]))
        self.assertEqual([row for row in rows["autopilot_task"] if row["novel_id"] == self.other_id], before)

    def test_native_kernel_response_lost_retry_does_not_repeat_into_third_chapter(self):
        self._finish()
        self._finish(self.other_id, self.other_task)
        with self.sessions() as db:
            for index in range(3):
                repos.OutlineRepo(db).create(self.novel_id, title=f"本章{index}", sort_order=index)
        first_started, first_release = threading.Event(), threading.Event()
        second_started, second_release = threading.Event(), threading.Event()
        self.addCleanup(first_release.set)
        self.addCleanup(second_release.set)
        contexts, calls = [], []

        def stream(*_args, **_kwargs):
            number = len(calls)
            calls.append(number)
            yield f"受控真实第{number}章正文。"
            if number < 2:
                (first_started if number == 0 else second_started).set()
                if not (first_release if number == 0 else second_release).wait(5):
                    raise RuntimeError("内核回归门闩超时")

        def context(_db, _novel, directive, *_args, **_kwargs):
            contexts.append(directive)
            return "受控上下文"
        with patch.object(auto_pilot.llm, "stream_complete", side_effect=stream), \
                patch.object(auto_pilot, "build_context", side_effect=context), \
                patch.object(auto_pilot, "review_story", return_value=StoryCheckResult()), \
                patch.object(auto_pilot, "consistency_check", return_value={"conflicts": []}), \
                patch.object(auto_pilot, "score_tension", return_value=5), \
                patch.object(auto_pilot, "check_drift", return_value=None):
            new_task = str(uuid.uuid4())
            self.manager.start(self.novel_id, new_task)
            self.assertTrue(first_started.wait(3))
            base = f"/api/v1/novels/{self.novel_id}/autopilot/tasks/{new_task}"
            key = str(uuid.uuid4())
            response_lost_to_author = self._send(key, "只约束下一章的原构思", base)
            self.assertEqual(response_lost_to_author.status_code, 200)  # 丢弃作者未收到的首次回应。
            first_release.set()
            self.assertTrue(second_started.wait(3))
            replay = self._send(key, "只约束下一章的原构思", base)
            self.assertTrue(replay.json()["receipt_replayed"])
            second_release.set()
            self.assertTrue(self.manager.wait(new_task, timeout=5))
        self.assertEqual(contexts, ["", "只约束下一章的原构思", ""])
        with self.sessions() as db:
            chapters = repos.ChapterRepo(db).list(self.novel_id)
            self.assertEqual([row.directive for row in chapters], contexts)


if __name__ == "__main__":
    unittest.main()
