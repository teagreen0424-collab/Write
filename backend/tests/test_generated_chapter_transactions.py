"""生成新章的最终短事务：真模型等待、物理SQLite、来源与动作原子性。"""
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Lock, Thread
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from app.application import chat_actions, writing
from app.infrastructure import models, repository as repos
from app.infrastructure.db import get_db
from app.interfaces.api.v1 import chapters, conversations
from app.interfaces.schemas import WriteChapterRequest


def frames(body):
    return [json.loads(line[5:].strip()) for line in body.splitlines() if line.startswith("data:")]


class GeneratedChapterTransactionTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "generation.db"
        self.engine = create_engine(f"sqlite:///{self.path.as_posix()}", connect_args={"check_same_thread": False, "timeout": .2})
        self.addCleanup(self.engine.dispose)
        models.Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        with self.sessions() as db:
            db.add_all([models.Novel(id="work", title="当前作品"), models.Novel(id="foreign", title="无关作品")])
            db.add(models.Conversation(id="thread", novel_id="work", title="作品讨论"))
            db.add_all([models.Message(id="source-a", conversation_id="thread", role="assistant", content="构思A"),
                models.Message(id="source-b", conversation_id="thread", role="assistant", content="构思B"),
                models.Chapter(id="prior", novel_id="work", seq=1, title="原前章", content="原前章事实", directive="原指令"),
                models.Chapter(id="foreign-chapter", novel_id="foreign", seq=1, content="他作不动")])
            db.commit()
        app = FastAPI()
        app.include_router(chapters.router, prefix="/api/v1")
        app.include_router(conversations.router, prefix="/api/v1")

        def dependency():
            with self.sessions() as db:
                yield db

        app.dependency_overrides[get_db] = dependency
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        for module in (chapters, conversations):
            mocked = patch.object(module, "SessionLocal", self.sessions)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.ordinary = "/api/v1/novels/work/chapters/write"
        self.chat = "/api/v1/conversations/thread/messages/source-a/chapter/write"
        self.base = "/api/v1/novels/work/chapters"
        self.body = " 生成候选😀\x00\r\n "

    def rows(self):
        with self.sessions() as db:
            return {table.name: [dict(row) for row in db.execute(select(table)).mappings()]
                for table in models.Base.metadata.sorted_tables}

    def request(self, path, **fields):
        return self.client.post(path, json={"title": "新候选", "directive": "原意图", **fields})

    def assert_conflict(self, values):
        self.assertEqual(values[0], {"type": "token", "content": self.body})
        error = values[-1]
        self.assertEqual(error["type"], "error", values)
        self.assertEqual((error["type"], error["code"], error["status"], error["saved"]),
                         ("error", "chapter_write_conflict", 409, False))
        self.assertIn("候选", error["hint"])
        self.assertNotIn("INSERT INTO", error["message"])
        self.assertNotIn("done", [row["type"] for row in values])

    def paused_generation(self, path, mutation):
        entered, release, result = Event(), Event(), {}

        def provider(*_args, **_kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            yield self.body

        def worker():
            try:
                result["response"] = self.request(path)
            except BaseException as exc:
                result["exception"] = repr(exc)

        with patch.object(writing.llm, "stream_complete", side_effect=provider):
            thread = Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                # Real independent connection can own a write transaction during model wait.
                observer = sqlite3.connect(self.path, timeout=.1)
                try:
                    observer.execute("BEGIN IMMEDIATE")
                    observer.rollback()
                finally:
                    observer.close()
                mutation()
            finally:
                release.set()
                thread.join(8)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("exception", result)
        return frames(result["response"].text)

    def test_two_default_ordinary_intents_commit_one_and_conflict_one(self):
        self.concurrent(self.ordinary, self.ordinary)

    def test_two_different_chat_sources_commit_one_action_and_conflict_one(self):
        self.concurrent(self.chat, self.chat.replace("source-a", "source-b"))

    def test_same_chat_source_concurrent_completion_replays_before_source_conflict(self):
        self.concurrent(self.chat, self.chat, same_source=True)

    def concurrent(self, first, second, same_source=False):
        entered, barrier, lock = Event(), Barrier(2), Lock()
        count, values, failures = [], [], []

        def provider(*_args, **_kwargs):
            with lock:
                count.append(1)
                if len(count) == 2:
                    entered.set()
            barrier.wait(5)
            yield self.body

        def worker(path):
            try:
                response = self.request(path)
                values.append(frames(response.text))
            except BaseException as exc:
                failures.append(repr(exc))

        with patch.object(writing.llm, "stream_complete", side_effect=provider):
            workers = [Thread(target=worker, args=(path,)) for path in (first, second)]
            for thread in workers:
                thread.start()
            for thread in workers:
                thread.join(8)
                self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(entered.is_set())
        done = [row[-1] for row in values if row[-1]["type"] == "done"]
        self.assertEqual(len(done), 2 if same_source else 1, values)
        if same_source:
            self.assertEqual(done[0]["chapter"]["id"], done[1]["chapter"]["id"])
        else:
            self.assert_conflict(next(row for row in values if row[-1]["type"] == "error"))
        rows = self.rows()
        self.assertEqual(sorted(row["seq"] for row in rows["chapter"] if row["novel_id"] == "work"), [1, 2])
        self.assertEqual(len(rows["chat_action"]), 1 if first == self.chat else 0)

    def test_ordinary_and_chat_manual_create_while_model_waits_leave_no_generated_chapter(self):
        for path in (self.ordinary, self.chat):
            with self.subTest(path=path):
                # Restore clean starting work between subcases, preserving its original chapter.
                with self.sessions() as db:
                    db.execute(models.Chapter.__table__.delete().where(models.Chapter.id != "prior", models.Chapter.novel_id == "work"))
                    db.commit()
                def manual():
                    response = self.client.post(self.base, json={"title": "作者手写", "content": "实际手写"})
                    self.assertEqual(response.status_code, 201)
                self.assert_conflict(self.paused_generation(path, manual))
                self.assertEqual(len([row for row in self.rows()["chapter"] if row["novel_id"] == "work"]), 2)
                self.assertEqual(self.rows()["chat_action"], [])

    def test_deleted_and_replaced_same_seq_latest_identity_conflicts(self):
        def replace():
            self.assertEqual(self.client.delete(self.base + "/prior").status_code, 204)
            replacement = self.client.post(self.base, json={"seq": 1, "title": "同序号替代", "content": "作者新来源"})
            self.assertEqual(replacement.status_code, 201)
        self.assert_conflict(self.paused_generation(self.ordinary, replace))
        self.assertEqual([row["seq"] for row in self.rows()["chapter"] if row["novel_id"] == "work"], [1])

    def test_latest_delete_with_existing_same_seq_predecessor_conflicts(self):
        with self.sessions() as db:
            db.add(models.Chapter(id="same-seq-latest", novel_id="work", seq=1, title="作者明确同序号另一稿"))
            db.commit()
        self.assert_conflict(self.paused_generation(self.ordinary, lambda: self.client.delete(self.base + "/same-seq-latest")))
        self.assertEqual([row["seq"] for row in self.rows()["chapter"] if row["novel_id"] == "work"], [1])

    def test_unrelated_work_change_does_not_conflict_or_override_previous_author(self):
        def mutate():
            self.assertEqual(self.client.post("/api/v1/novels/foreign/chapters", json={"content": "他作新增"}).status_code, 201)
        values = self.paused_generation(self.ordinary, mutate)
        self.assertEqual(values[-1]["type"], "done", values)
        self.assertEqual(values[-1]["chapter"]["seq"], 2)
        rows = self.rows()["chapter"]
        self.assertEqual(next(row for row in rows if row["id"] == "prior")["content"], "原前章事实")
        self.assertEqual(next(row for row in rows if row["id"] == "foreign-chapter")["content"], "他作不动")

    def test_previous_author_change_conflicts_without_overriding_saved_author(self):
        def mutate():
            self.assertEqual(self.client.put(self.base + "/prior", json={"title": "新前章标题", "content": "新前章事实",
                "directive": "新作者指令", "summary": "作者摘要"}).status_code, 200)
        values = self.paused_generation(self.ordinary, mutate)
        self.assert_conflict(values)
        rows = self.rows()
        current = next(row for row in rows["chapter"] if row["id"] == "prior")
        self.assertEqual((current["title"], current["content"], current["directive"]), ("新前章标题", "新前章事实", "新作者指令"))
        self.assertEqual(len(rows["chapter_revision"]), 1)
        self.assertEqual(rows["chapter_revision"][0]["content"], "原前章事实")
        self.assertEqual(len([row for row in rows["chapter"] if row["novel_id"] == "work"]), 1)
        self.assertEqual(next(row for row in rows["chapter"] if row["id"] == "foreign-chapter")["content"], "他作不动")

    def test_same_chat_source_replay_after_newer_chapter_is_readonly_zero_model(self):
        with patch.object(writing.llm, "stream_complete", return_value=iter([self.body])):
            first = frames(self.request(self.chat).text)[-1]
        self.assertEqual(first["type"], "done")
        self.client.post(self.base, json={"content": "后续手写"})
        before = self.rows()
        with patch.object(writing.llm, "stream_complete", side_effect=AssertionError("replay cannot call model")) as provider:
            replay = frames(self.request(self.chat).text)
        provider.assert_not_called()
        self.assertEqual([row["type"] for row in replay], ["done"])
        self.assertEqual(replay[0]["chapter"]["id"], first["chapter"]["id"])
        self.assertEqual(self.rows(), before)

    def test_readonly_cached_chat_binding_cannot_replay_after_actual_scope_change(self):
        with patch.object(writing.llm, "stream_complete", return_value=iter([self.body])):
            saved = frames(self.request(self.chat).text)[-1]["chapter"]
        with self.sessions() as db:
            cached = db.get(models.Conversation, "thread")
            cached_chapter = db.get(models.Chapter, saved["id"])
            self.assertEqual(cached.novel_id, "work")
            with self.sessions() as changed:
                changed.execute(models.Conversation.__table__.update().where(
                    models.Conversation.id == "thread").values(novel_id="foreign"))
                changed.commit()
            before = self.rows()
            with db.no_autoflush, patch.object(writing.llm, "stream_complete") as provider:
                values = [json.loads(line[5:]) for line in chat_actions.write_chapter(
                    db, "thread", "source-a", WriteChapterRequest())]
            provider.assert_not_called()
            self.assertEqual([row["type"] for row in values], ["error"])
            self.assertEqual(values[0]["status"], 409)
            self.assertNotIn(self.body, values[0]["message"])
            self.assertEqual(self.rows(), before)
            self.assertNotIn(cached_chapter, db.dirty)

    def test_readonly_cached_chat_replay_returns_actual_current_chapter_fields(self):
        with patch.object(writing.llm, "stream_complete", return_value=iter([self.body])):
            saved = frames(self.request(self.chat).text)[-1]["chapter"]
        with self.sessions() as db:
            cached = db.get(models.Chapter, saved["id"])
            self.assertEqual(cached.title, "新候选")
            changed = self.client.put(self.base + "/" + saved["id"], json={"title": "作者已改标题", "content": "真实当前稿"})
            self.assertEqual(changed.status_code, 200)
            before = self.rows()
            with patch.object(writing.llm, "stream_complete") as provider:
                values = [json.loads(line[5:]) for line in chat_actions.write_chapter(
                    db, "thread", "source-a", WriteChapterRequest())]
            provider.assert_not_called()
            self.assertEqual(values, [{"type": "done", "chapter": changed.json()}])
            self.assertEqual(self.rows(), before)

    def test_explicit_manual_duplicate_seq_and_shared_core_insert_contract_remain_legal(self):
        for _index in range(2):
            self.assertEqual(self.client.post(self.base, json={"seq": 2, "content": "作者明确同序号稿"}).status_code, 201)
        with self.sessions() as db:
            db.execute(text("UPDATE chapter SET seq=seq+1 WHERE novel_id='work' AND seq>=2"))
            created = repos.ChapterRepo(db).create("work", seq=2, content="显式自动驾驶插入")
            self.assertEqual(created.seq, 2)
        self.assertEqual(sorted(row["seq"] for row in self.rows()["chapter"] if row["novel_id"] == "work"), [1, 2, 3, 3])

    def test_dirty_new_deleted_flushed_core_callers_rejected_before_read_without_rollback(self):
        for operation in ("ordinary", "chat"):
            for kind in ("dirty", "new", "deleted", "flushed", "core"):
                with self.subTest(operation=operation, kind=kind), self.sessions(autoflush=True) as db:
                    chapter = db.get(models.Chapter, "prior")
                    if kind == "new":
                        db.add(models.Character(novel_id="work", name="调用方未提交人物"))
                    elif kind == "deleted":
                        db.delete(chapter)
                    elif kind == "core":
                        db.execute(text("UPDATE chapter SET title='调用方Core未提交' WHERE id='prior'"))
                    else:
                        chapter.title = "调用方未提交标题"
                        if kind == "flushed":
                            db.flush()
                    with patch.object(db, "flush") as flush, patch.object(db, "commit") as commit, \
                            patch.object(db, "rollback") as rollback, patch.object(writing.llm, "stream_complete") as provider:
                        stream = writing.write_chapter(db, "work") if operation == "ordinary" else chat_actions.write_chapter(
                            db, "thread", "source-a", WriteChapterRequest())
                        values = [json.loads(line[5:]) for line in stream]
                        self.assertEqual(values[-1]["type"], "error")
                        provider.assert_not_called()
                        flush.assert_not_called()
                        commit.assert_not_called()
                        rollback.assert_not_called()
                    self.assertTrue(db.new or db.dirty or db.deleted or db.connection().connection.driver_connection.in_transaction)
                    db.rollback()

    def test_caller_changes_made_during_model_wait_are_not_committed_or_rolled_back(self):
        with self.sessions() as db:
            def provider(*_args, **_kwargs):
                db.add(models.Character(novel_id="work", name="调用方中途人物"))
                yield self.body
            with patch.object(writing.llm, "stream_complete", side_effect=provider), \
                    patch.object(db, "rollback") as rollback, patch.object(db, "commit") as commit:
                values = [json.loads(line[5:]) for line in writing.write_chapter(db, "work")]
                self.assert_conflict(values)
                rollback.assert_not_called()
                commit.assert_not_called()
            self.assertEqual(len(db.new), 1)
            db.commit()
        self.assertEqual(len(self.rows()["character"]), 1)
        self.assertEqual(len([row for row in self.rows()["chapter"] if row["novel_id"] == "work"]), 1)

    def test_real_chapter_trigger_rolls_back_chat_action_and_avoids_sql_payload_error(self):
        with self.engine.begin() as db:
            db.exec_driver_sql("CREATE TRIGGER refuse_generated BEFORE INSERT ON chapter WHEN NEW.title='新候选' BEGIN SELECT RAISE(ABORT,'controlled trigger'); END")
        before = self.rows()
        with patch.object(writing.llm, "stream_complete", return_value=iter([self.body])):
            values = frames(self.request(self.chat).text)
        self.assertEqual(values[-1]["type"], "error")
        self.assertNotIn("INSERT INTO", values[-1]["message"])
        self.assertNotIn(self.body, values[-1]["message"])
        self.assertEqual(self.rows(), before)

    def test_real_sqlite_busy_has_safe_conflict_and_no_generated_write(self):
        locked = []
        def provider(*_args, **_kwargs):
            blocker = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
            blocker.execute("BEGIN IMMEDIATE")
            locked.append(blocker)
            yield self.body
        try:
            with patch.object(writing.llm, "stream_complete", side_effect=provider):
                values = frames(self.request(self.ordinary).text)
            self.assert_conflict(values)
            self.assertNotIn("database is locked", values[-1]["message"])
        finally:
            for blocker in locked:
                blocker.rollback()
                blocker.close()
        self.assertEqual(len([row for row in self.rows()["chapter"] if row["novel_id"] == "work"]), 1)

    def test_initial_real_exclusive_read_failure_returns_no_sql_and_calls_no_model(self):
        blocker = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        blocker.execute("BEGIN EXCLUSIVE")
        try:
            for path in (self.ordinary, self.chat):
                with self.subTest(path=path), patch.object(writing.llm, "stream_complete") as provider:
                    values = frames(self.request(path).text)
                    self.assertEqual([row["type"] for row in values], ["error"])
                    self.assertEqual(values[0]["status"], 500)
                    self.assertNotIn("SELECT", values[0]["message"])
                    self.assertNotIn("database is locked", values[0]["message"])
                    self.assertNotIn("parameters", values[0]["message"])
                    provider.assert_not_called()
        finally:
            blocker.rollback()
            blocker.close()

    def test_deleted_chat_source_or_outline_during_model_never_creates_orphan(self):
        self.assertEqual(self.paused_generation(self.chat, lambda: self.client.delete("/api/v1/conversations/thread"))[-1]["type"], "error")
        self.assertEqual(self.rows()["chat_action"], [])
        self.assertEqual(len([row for row in self.rows()["chapter"] if row["novel_id"] == "work"]), 1)
        with self.sessions() as db:
            db.add(models.OutlineNode(id="bound", novel_id="work", level="chapter", title="所选目标"))
            db.commit()
        entered, release, responses = Event(), Event(), []
        def provider(*_args, **_kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            yield self.body
        with patch.object(writing.llm, "stream_complete", side_effect=provider):
            thread = Thread(target=lambda: responses.append(self.request(self.ordinary, outline_node_id="bound")))
            thread.start()
            self.assertTrue(entered.wait(5))
            with self.sessions() as db:
                db.execute(models.OutlineNode.__table__.delete().where(models.OutlineNode.id == "bound"))
                db.commit()
            release.set()
            thread.join(8)
        self.assertFalse(thread.is_alive())
        self.assert_conflict(frames(responses[0].text))

    def test_actual_commit_then_exclusive_lock_has_done_and_zero_postcommit_select(self):
        for path in (self.ordinary, self.chat):
            with self.subTest(path=path):
                marker, blockers, statements = {}, [], []
                original = self.sessions.class_.commit

                def remember(db, *_args):
                    if any(isinstance(row, models.Chapter) and row.title == "新候选" for row in db.new):
                        db.info["generation-lock-target"] = True

                def committed(db):
                    original(db)
                    if db.info.pop("generation-lock-target", False):
                        blocker = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
                        blocker.execute("BEGIN EXCLUSIVE")
                        blockers.append(blocker)
                        marker["after"] = len(statements)

                def capture(_conn, _cursor, statement, *_args):
                    statements.append(statement)

                event.listen(self.sessions.class_, "before_flush", remember)
                event.listen(self.engine, "before_cursor_execute", capture)
                try:
                    with patch.object(self.sessions.class_, "commit", new=committed), \
                            patch.object(writing.llm, "stream_complete", return_value=iter([self.body])):
                        values = frames(self.request(path).text)
                    self.assertIn("after", marker)
                    self.assertEqual(values[-1]["type"], "done", values)
                    self.assertEqual(values[-1]["chapter"]["content"], self.body)
                    self.assertEqual(statements[marker["after"]:], [])
                finally:
                    event.remove(self.sessions.class_, "before_flush", remember)
                    event.remove(self.engine, "before_cursor_execute", capture)
                    for blocker in blockers:
                        blocker.rollback()
                        blocker.close()


if __name__ == "__main__":
    unittest.main()
