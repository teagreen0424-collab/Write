"""全局会话转作品：真实HTTP/SQLite原子性、并发与提交后的冻结响应。"""
from contextlib import closing
import sqlite3
from threading import Barrier, Thread
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import event, inspect, text

from app.application import chat
from app.infrastructure import models
import test_generated_chapter_transactions as transactions


class ConversationPromotionTest(unittest.TestCase):
    rows = transactions.GeneratedChapterTransactionTest.rows

    def setUp(self):
        transactions.GeneratedChapterTransactionTest.setUp(self)
        with self.sessions() as db:
            db.add(models.Conversation(id="global", title="原全局构思"))
            db.add(models.Message(id="original-global-message", conversation_id="global", role="user", content="作者原聊天构思😀\0"))
            db.add(models.ChatAction(message_id="source-a", novel_id="work", action_type="settings", status="done", result_id="original-result", result_summary={"kept": True}))
            db.commit()
        self.path_promote = "/api/v1/conversations/global/promote"
        self.payload = {"title": "原作品标题😀", "genre": "奇幻", "summary": " 原作者摘要\0\r\n "}

    def promote(self, path=None, **changes):
        return self.client.post(path or self.path_promote, json={**self.payload, **changes})

    def global_row(self):
        return next(row for row in self.rows()["conversation"] if row["id"] == "global")

    def test_success_commits_one_novel_and_binding_with_original_data_preserved(self):
        before = self.rows()
        response = self.promote()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(set(response.json()), {column.key for column in models.Novel.__table__.columns})
        for key, value in self.payload.items():
            self.assertEqual(response.json()[key], value)
        after = self.rows()
        self.assertEqual(len(after["novel"]), len(before["novel"]) + 1)
        self.assertEqual(self.global_row()["novel_id"], response.json()["id"])
        for name in models.Base.metadata.tables:
            if name not in {"novel", "conversation"}:
                self.assertEqual(after[name], before[name], name)
        self.assertEqual([row for row in after["novel"] if row["id"] in {item["id"] for item in before["novel"]}], before["novel"])

    def test_already_bound_rejection_never_creates_unrelated_novel(self):
        before = self.rows()
        response = self.promote("/api/v1/conversations/thread/promote")
        self.assertEqual(response.status_code, 400)
        self.assertIn("已绑定作品", response.json()["detail"])
        self.assertEqual(self.rows(), before)

    def test_unknown_success_response_then_original_retry_keeps_only_original_binding(self):
        first = self.promote()
        self.assertEqual(first.status_code, 201)
        before = self.rows()
        second = self.promote()
        self.assertEqual(second.status_code, 400)
        self.assertEqual(self.global_row()["novel_id"], first.json()["id"])
        self.assertEqual(self.rows(), before)

    def test_missing_conversation_rejection_is_404_without_writes(self):
        before = self.rows()
        response = self.promote("/api/v1/conversations/missing/promote")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.rows(), before)

    def test_real_novel_or_binding_insert_trigger_failure_rolls_back_all_and_hides_sql(self):
        for table, operation in (("novel", "INSERT"), ("conversation", "UPDATE")):
            with self.subTest(table=table):
                with self.engine.begin() as db:
                    db.exec_driver_sql(f"CREATE TRIGGER refuse_promotion BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'controlled promotion storage failure'); END")
                before = self.rows()
                try:
                    response = self.promote()
                    self.assertEqual(response.status_code, 500)
                    self.assertIsInstance(response.json()["detail"], str)
                    self.assertIn("核对会话归属", response.json()["detail"])
                    for secret in ("INSERT INTO", "UPDATE conversation", "parameters", "controlled promotion storage failure", self.payload["summary"]):
                        self.assertNotIn(secret, response.json()["detail"])
                    self.assertEqual(self.rows(), before)
                finally:
                    with self.engine.begin() as db:
                        db.exec_driver_sql("DROP TRIGGER refuse_promotion")

    def test_two_native_sessions_promote_one_conversation_only_once(self):
        started, attempting = Barrier(2), Barrier(2)
        responses, attempts = [], []
        def before(_connection, _cursor, statement, *_args):
            if statement == "BEGIN IMMEDIATE":
                attempts.append(statement)
                attempting.wait(5)  # 暂停在真实锁执行之前，不在已持锁的SELECT内等待。
        def worker(label):
            started.wait(5)
            responses.append(self.promote(title=label))
        event.listen(self.engine, "before_cursor_execute", before)
        try:
            workers = [Thread(target=worker, args=(name,)) for name in ("作者意图A", "作者意图B")]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(8)
                self.assertFalse(worker.is_alive())
        finally:
            event.remove(self.engine, "before_cursor_execute", before)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sorted(row.status_code for row in responses), [201, 400])
        winner = next(row.json() for row in responses if row.status_code == 201)
        self.assertEqual(self.global_row()["novel_id"], winner["id"])
        self.assertEqual(len(self.rows()["novel"]), 3)

    def test_readonly_cached_binding_is_rechecked_from_actual_columns(self):
        with self.sessions(autoflush=True) as db:
            cached = db.get(models.Conversation, "global")
            self.assertIsNone(cached.novel_id)
            self.assertEqual(self.client.patch("/api/v1/conversations/global", json={"novel_id": "work"}).status_code, 200)
            before = self.rows()
            with db.no_autoflush:
                with self.assertRaises(HTTPException) as raised:
                    chat.promote_conversation(db, "global", **self.payload)
            self.assertEqual(raised.exception.status_code, 400)
            self.assertEqual(self.rows(), before)

    def test_caller_dirty_new_deleted_flushed_core_are_rejected_without_touching_transaction(self):
        for kind in ("dirty", "new", "deleted", "flushed", "core"):
            with self.subTest(kind=kind), self.sessions(autoflush=True) as db:
                chapter = db.get(models.Chapter, "prior")
                if kind == "new":
                    db.add(models.Character(novel_id="work", name="作者调用方新人物"))
                elif kind == "deleted":
                    db.delete(chapter)
                elif kind == "core":
                    db.execute(text("UPDATE chapter SET title='调用方Core未提交' WHERE id='prior'"))
                else:
                    chapter.title = "调用方未提交标题"
                    if kind == "flushed":
                        db.flush()
                with patch.object(db, "flush") as flush, patch.object(db, "commit") as commit, patch.object(db, "rollback") as rollback:
                    with self.assertRaises(HTTPException) as raised:
                        chat.promote_conversation(db, "global", **self.payload)
                    self.assertEqual(raised.exception.status_code, 409)
                    flush.assert_not_called()
                    commit.assert_not_called()
                    rollback.assert_not_called()
                # 并非只检查mock：原调用方修改在拒绝后仍能真正提交。
                db.commit()
                self.assertIsNone(self.global_row()["novel_id"])
                self.assertEqual(len(self.rows()["novel"]), 2)
                if kind == "new":
                    self.assertEqual(len(self.rows()["character"]), 1)
                elif kind == "deleted":
                    self.assertNotIn("prior", {row["id"] for row in self.rows()["chapter"]})
                else:
                    self.assertIn("调用方", next(row for row in self.rows()["chapter"] if row["id"] == "prior")["title"])
                # 为下一种调用方状态恢复测试种子；不影响事务断言。
                with self.sessions() as reset:
                    if kind == "deleted":
                        reset.add(models.Chapter(id="prior", novel_id="work", seq=1, title="原前章", content="原前章事实"))
                    reset.commit()

    def test_clean_read_autobegin_and_explicit_no_autoflush_are_accepted(self):
        with self.sessions(autoflush=True) as db:
            cached = db.get(models.Conversation, "global")
            self.assertIsNone(cached.novel_id)
            self.assertFalse(db.connection().connection.driver_connection.in_transaction)
            with db.no_autoflush:
                saved = chat.promote_conversation(db, "global", **self.payload)
            self.assertIsInstance(saved, models.Novel)
            self.assertFalse(inspect(saved).expired_attributes)
            self.assertFalse(db.dirty or db.new or db.deleted)
            self.assertEqual(saved.summary, self.payload["summary"])
            self.assertEqual(self.global_row()["novel_id"], saved.id)

    def test_true_sqlite_busy_returns_safe_conflict_without_new_novel(self):
        before = self.rows()
        blocker = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            response = self.promote()
            self.assertEqual(response.status_code, 409)
            for token in ("database is locked", "parameters", self.payload["summary"]):
                self.assertNotIn(token, response.json()["detail"])
        finally:
            blocker.rollback()
            blocker.close()
        self.assertEqual(self.rows(), before)

    def test_actual_postcommit_exclusive_lock_still_returns_201_with_zero_sql(self):
        original, marker, statements, blockers = self.sessions.class_.commit, {}, [], []
        def remember(db, *_args):
            novel = next((row for row in db.new if isinstance(row, models.Novel)), None)
            if novel is not None:
                db.info["promotion-new-novel"] = novel
        def commit(db):
            original(db)
            if db.info.pop("promotion-new-novel", None) is not None:
                blocker = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
                blocker.execute("BEGIN EXCLUSIVE")
                blockers.append(blocker)
                marker["after"] = len(statements)
        def capture(_connection, _cursor, statement, *_args):
            statements.append(statement)
        event.listen(self.sessions.class_, "before_flush", remember)
        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            with patch.object(self.sessions.class_, "commit", new=commit):
                response = self.promote()
            self.assertIn("after", marker)
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json()["summary"], self.payload["summary"])
            self.assertEqual(statements[marker["after"]:], [])
        finally:
            event.remove(self.sessions.class_, "before_flush", remember)
            event.remove(self.engine, "before_cursor_execute", capture)
            for blocker in blockers:
                blocker.rollback()
                blocker.close()
        self.assertEqual(self.global_row()["novel_id"], response.json()["id"])

    def test_legacy_unknown_columns_and_foreign_rows_are_preserved(self):
        with self.engine.begin() as db:
            db.exec_driver_sql("ALTER TABLE conversation ADD COLUMN legacy_note TEXT DEFAULT '保留会话旧列'")
            db.exec_driver_sql("ALTER TABLE novel ADD COLUMN legacy_note TEXT DEFAULT '保留作品旧列'")
            db.exec_driver_sql("UPDATE conversation SET legacy_note='原全局私有列' WHERE id='global'")
            db.exec_driver_sql("UPDATE novel SET legacy_note='其他作品私有值' WHERE id='foreign'")
        def raw():
            with closing(sqlite3.connect(self.path)) as db:
                db.row_factory = sqlite3.Row
                return {name: [dict(row) for row in db.execute(f"SELECT * FROM {name} ORDER BY rowid")]
                    for name in ("novel", "conversation")}
        before = raw()
        response = self.promote()
        self.assertEqual(response.status_code, 201)
        after = raw()
        self.assertEqual([row for row in after["novel"] if row["id"] != response.json()["id"]], before["novel"])
        self.assertEqual(next(row for row in after["conversation"] if row["id"] == "global")["legacy_note"], "原全局私有列")
        self.assertEqual([row for row in after["conversation"] if row["id"] != "global"], [row for row in before["conversation"] if row["id"] != "global"])
