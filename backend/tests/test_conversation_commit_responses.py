"""会话创建/改名/归组：提交前真实五列，提交后不再用读取否认保存。"""
from contextlib import closing, nullcontext
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.application import chat
from app.infrastructure import models, repository as repos
from app.infrastructure.db import get_db
from app.interfaces.api.v1 import conversations


def frames(body):
    import json
    return [json.loads(line[5:].strip()) for line in body.splitlines() if line.startswith("data:")]


class ConversationCommitResponseTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "会话真实保存.sqlite"
        self.engine = create_engine(f"sqlite:///{self.path.as_posix()}",
            connect_args={"check_same_thread": False, "timeout": .03})
        self.addCleanup(self.engine.dispose)
        models.Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=True)
        with self.sessions() as db:
            db.add_all([models.Novel(id="work", title="作者作品"), models.Novel(id="foreign", title="无关作品")])
            db.add_all([models.Conversation(id="global", novel_id=None, title="原全局会话"),
                models.Conversation(id="bound", novel_id="work", title="原作品会话"),
                models.Conversation(id="untitled", novel_id=None, title=""),
                models.Conversation(id="foreign-thread", novel_id="foreign", title="无关会话")])
            db.add_all([models.Message(id="old-global", conversation_id="global", role="assistant", content="原全局历史😀\0", meta={"原": True}),
                models.Message(id="foreign-message", conversation_id="foreign-thread", role="assistant", content="其他历史不动😀\0"),
                models.Chapter(id="prior", novel_id="work", seq=1, title="调用方原章", content="原文😀\0"),
                models.Chapter(id="foreign-chapter", novel_id="foreign", seq=1, title="其他作品原章", content="其他正文不动😀\0")])
            db.commit()
        app = FastAPI()
        app.include_router(conversations.router, prefix="/api/v1")

        def dependency():
            with self.sessions() as db:
                yield db

        app.dependency_overrides[get_db] = dependency
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        override = patch.object(conversations, "SessionLocal", self.sessions)
        override.start()
        self.addCleanup(override.stop)
        self.title = "\n　真实会话作者名😀\0　\n"

    def rows(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {name: [dict(row) for row in db.execute(f'SELECT * FROM "{name}" ORDER BY rowid')] for name in tables}

    def schema(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()

    def actual_metadata_postcommit_lock(self, mode):
        before, statements, blockers, marker = self.rows(), [], [], {}
        original_commit = self.sessions.class_.commit
        payload = {"novel_id": None} if mode == "create" else {"title": self.title} if mode == "rename" else {"novel_id": "work"}
        target_id = "bound" if mode == "rename" else "global"

        def remember(db, *_args):
            targeted = (any(isinstance(obj, models.Conversation) for obj in db.new) if mode == "create"
                else any(isinstance(obj, models.Conversation) and obj.id == target_id for obj in db.dirty))
            if targeted:
                db.info["conversation-response-target"] = True

        def commit(db):
            original_commit(db)
            if db.info.pop("conversation-response-target", False):
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
                response = (self.client.post("/api/v1/conversations", json=payload) if mode == "create"
                    else self.client.patch(f"/api/v1/conversations/{target_id}", json=payload))
            self.assertIn("after", marker)
            self.assertEqual(response.status_code, 201 if mode == "create" else 200, response.text)
            self.assertEqual(statements[marker["after"]:], [])
            body = response.json()
            self.assertEqual(set(body), {column.key for column in models.Conversation.__table__.columns})
            self.assertEqual(len(body), 5)
            self.assertTrue(all(body[key] == value for key, value in payload.items()))
        finally:
            event.remove(self.sessions.class_, "before_flush", remember)
            event.remove(self.engine, "before_cursor_execute", capture)
            for blocker in blockers:
                blocker.rollback()
                blocker.close()
        after = self.rows()
        self.assertEqual(next(row for row in after["conversation"] if row["id"] == body["id"]), body)
        self.assertEqual(len(after["conversation"]), len(before["conversation"]) + (mode == "create"))
        self.assertEqual({name: value for name, value in after.items() if name != "conversation"},
                         {name: value for name, value in before.items() if name != "conversation"})

    def test_native_create_postcommit_exclusive_returns201_five_columns_zero_sql(self):
        self.actual_metadata_postcommit_lock("create")

    def test_native_rename_postcommit_exclusive_returns200_five_columns_zero_sql(self):
        self.actual_metadata_postcommit_lock("rename")

    def test_native_global_move_postcommit_exclusive_preserves_history_and_returns200(self):
        self.actual_metadata_postcommit_lock("move")

    def shared_caller_states(self, mode):
        for expire in (True, False):
            for kind in ("new", "dirty", "deleted", "flushed", "core"):
                with self.subTest(expire_on_commit=expire, kind=kind), self.sessions(expire_on_commit=expire) as db:
                    key = f"{mode}-{expire}-{kind}"
                    caller = models.Chapter(id=key, novel_id="work", seq=2, title="调用方原稿", content="作者稿😀\0")
                    db.add(caller)
                    db.commit()
                    target = db.get(models.Conversation, "bound")
                    before = self.rows()
                    if kind == "new":
                        db.add(models.Character(id=key, novel_id="work", name="调用方新人物"))
                    elif kind == "deleted":
                        db.delete(caller)
                    elif kind == "core":
                        db.execute(text("UPDATE chapter SET title=:title WHERE id=:id"), {"title": "调用方Core稿", "id": key})
                    else:
                        caller.title = "调用方未提交稿"
                        if kind == "flushed":
                            db.flush()
                    added, committed, post_sql = [], [], []
                    original_commit = db.commit

                    def remember(_db, *_args):
                        added.extend(obj for obj in db.new if isinstance(obj, models.Conversation))

                    def commit():
                        original_commit()
                        committed.append(True)

                    def capture(_connection, _cursor, statement, *_args):
                        if committed:
                            post_sql.append(statement)

                    event.listen(db, "before_flush", remember)
                    event.listen(self.engine, "before_cursor_execute", capture)
                    try:
                        with db.no_autoflush, patch.object(db, "commit", side_effect=commit) as commit_call, patch.object(db, "rollback", wraps=db.rollback) as rollback:
                            saved = (repos.ConversationRepo(db).create(novel_id="work", title=self.title) if mode == "create"
                                else repos.ConversationRepo(db).update("bound", title=self.title))
                            commit_call.assert_called_once_with()
                            rollback.assert_not_called()
                        self.assertIs(saved, added[-1] if mode == "create" else target)
                        self.assertFalse(inspect(saved).expired_attributes or db.new or db.dirty or db.deleted)
                        self.assertEqual(saved.title, self.title)
                        self.assertEqual(post_sql, [])
                    finally:
                        event.remove(db, "before_flush", remember)
                        event.remove(self.engine, "before_cursor_execute", capture)
                    after = self.rows()
                    if kind == "new":
                        self.assertTrue(any(row["id"] == key for row in after["character"]))
                    elif kind == "deleted":
                        self.assertFalse(any(row["id"] == key for row in after["chapter"]))
                    else:
                        self.assertEqual(next(row for row in after["chapter"] if row["id"] == key)["title"],
                                         "调用方Core稿" if kind == "core" else "调用方未提交稿")
                    self.assertEqual({name: value for name, value in after.items() if name not in ("conversation", "chapter", "character")},
                                     {name: value for name, value in before.items() if name not in ("conversation", "chapter", "character")})

    def test_shared_create_commits_five_caller_states_both_expiration_modes_same_identity(self):
        self.shared_caller_states("create")

    def test_shared_update_commits_five_caller_states_both_expiration_modes_same_identity(self):
        self.shared_caller_states("update")

    def test_query_default_autoflush_noauto_and_false_preserve_actual_write_order(self):
        for mode in ("default", "noauto", "autoflush_false"):
            with self.subTest(mode=mode), self.sessions(autoflush=mode != "autoflush_false") as db:
                caller = db.get(models.Chapter, "prior")
                caller.title = f"实际调用方稿{mode}"
                statements = []

                def capture(_connection, _cursor, statement, *_args):
                    statements.append(statement)

                event.listen(self.engine, "before_cursor_execute", capture)
                try:
                    with db.no_autoflush if mode == "noauto" else nullcontext():
                        saved = repos.ConversationRepo(db).update("bound", title=f"改名{mode}")
                    self.assertFalse(inspect(saved).expired_attributes or db.dirty)
                finally:
                    event.remove(self.engine, "before_cursor_execute", capture)
                read = next(i for i, statement in enumerate(statements) if statement.startswith("SELECT conversation."))
                write = next(i for i, statement in enumerate(statements) if statement.startswith("UPDATE chapter"))
                self.assertLess(write, read) if mode == "default" else self.assertLess(read, write)
                self.assertEqual(next(row for row in self.rows()["chapter"] if row["id"] == "prior")["title"], caller.title)

    def test_same_target_core_cache_and_real_insert_trigger_return_actual_five_columns(self):
        with self.sessions(autoflush=False) as db:
            cached = db.get(models.Conversation, "global")
            old_title = cached.title
            db.execute(text("UPDATE conversation SET title=:title WHERE id='global'"), {"title": "同目标Core新名😀\0"})
            self.assertEqual(cached.title, old_title)
            db.execute(text("UPDATE chapter SET title='同次Core作者稿' WHERE id='prior'"))
            with db.no_autoflush:
                saved = repos.ConversationRepo(db).update("global", novel_id="work")
            self.assertIs(saved, cached)
            returned = {column.key: getattr(saved, column.key) for column in saved.__table__.columns}
            self.assertFalse(inspect(saved).expired_attributes or db.dirty)
        actual = self.rows()
        self.assertEqual(returned, next(row for row in actual["conversation"] if row["id"] == "global"))
        self.assertEqual(returned["title"], "同目标Core新名😀\0")
        self.assertEqual(next(row for row in actual["chapter"] if row["id"] == "prior")["title"], "同次Core作者稿")
        with self.engine.begin() as connection:
            connection.exec_driver_sql("CREATE TRIGGER actual_conv_title AFTER INSERT ON conversation BEGIN "
                "UPDATE conversation SET title='存储真实名' || char(0) || '😀' WHERE id=NEW.id; END")
        response = self.client.post("/api/v1/conversations", json={"novel_id": None})
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["title"], "存储真实名\0😀")
        self.assertEqual(body, next(row for row in self.rows()["conversation"] if row["id"] == body["id"]))

    def test_none_empty_raw_titles_defaults_and_binding_policy_are_unchanged(self):
        with self.sessions() as db:
            saved = repos.ConversationRepo(db).update("bound", title=None, novel_id=None)
            self.assertEqual((saved.title, saved.novel_id), ("原作品会话", "work"))
            saved = repos.ConversationRepo(db).update("bound", title="")
            self.assertEqual(saved.title, "")
            created = repos.ConversationRepo(db).create()
            self.assertEqual((created.title, created.novel_id), ("", None))
            self.assertTrue(created.id and created.created_at and created.updated_at)
        move = self.client.patch("/api/v1/conversations/global", json={"novel_id": "work", "title": self.title})
        self.assertEqual(move.status_code, 200)
        before = self.rows()
        self.assertEqual(self.client.patch("/api/v1/conversations/global", json={"novel_id": "foreign"}).status_code, 400)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.client.patch("/api/v1/conversations/global", json={"novel_id": "work"}).status_code, 200)
        self.assertEqual(self.client.patch("/api/v1/conversations/untitled", json={"novel_id": "missing"}).status_code, 404)
        self.assertEqual(self.client.patch("/api/v1/conversations/missing", json={"title": "名"}).status_code, 404)
        self.assertEqual(self.client.post("/api/v1/conversations", json={"novel_id": "missing"}).status_code, 404)
        self.assertEqual(before["message"], self.rows()["message"])

    def test_true_insert_update_trigger_failures_preserve_caller_rollback_contract(self):
        for mode in ("create", "update"):
            with self.subTest(mode=mode):
                with self.engine.begin() as connection:
                    connection.exec_driver_sql(f"CREATE TRIGGER refuse_conv BEFORE {'INSERT' if mode == 'create' else 'UPDATE'} "
                        "ON conversation BEGIN SELECT RAISE(ABORT,'真实会话写入拒绝'); END")
                before = self.rows()
                try:
                    with self.sessions() as db:
                        db.execute(text("UPDATE chapter SET title='调用方Core挂起' WHERE id='prior'"))
                        db.add(models.Character(id="pending", novel_id="work", name="调用方挂起人物"))
                        with self.assertRaises(SQLAlchemyError):
                            if mode == "create":
                                repos.ConversationRepo(db).create(novel_id=None, title=self.title)
                            else:
                                repos.ConversationRepo(db).update("bound", title=self.title)
                        self.assertFalse(db.is_active)
                        self.assertEqual(self.rows(), before)
                        db.rollback()  # 原共享方法不替调用方新加catch/rollback。
                    response = (self.client.post("/api/v1/conversations", json={"novel_id": None}) if mode == "create"
                        else self.client.patch("/api/v1/conversations/bound", json={"title": self.title}))
                    self.assertEqual(response.status_code, 500)
                    self.assertEqual(self.rows(), before)
                finally:
                    with self.engine.begin() as connection:
                        connection.exec_driver_sql("DROP TRIGGER refuse_conv")

    def test_legacy_first_naming_returns_frozen_metadata_before_later_context_read_failure(self):
        raw, locks, marker, saved_returns, post_within_update = "\n　首用户精确原稿😀\0　\n", [], {}, [], []
        original_commit, original_update = self.sessions.class_.commit, repos.ConversationRepo.update
        statements = []

        def remember(db, *_args):
            if any(isinstance(obj, models.Conversation) and obj.id == "untitled" and obj.title == raw.strip()[:20] for obj in db.dirty):
                db.info["legacy-title-save"] = True

        def commit(db):
            original_commit(db)
            if db.info.pop("legacy-title-save", False):
                lock = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
                lock.execute("BEGIN EXCLUSIVE")
                locks.append(lock)
                marker["after"] = len(statements)

        def observe_update(repo, *args, **kwargs):
            obj = original_update(repo, *args, **kwargs)
            saved_returns.append({column.key: getattr(obj, column.key) for column in obj.__table__.columns})
            post_within_update.extend(statements[marker["after"]:])
            return obj

        def capture(_connection, _cursor, statement, *_args):
            statements.append(statement)

        event.listen(self.sessions.class_, "before_flush", remember)
        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            with patch.object(self.sessions.class_, "commit", new=commit), patch.object(repos.ConversationRepo, "update", new=observe_update), patch.object(chat.llm, "stream_complete") as provider:
                response = self.client.post("/api/v1/conversations/untitled/messages", json={"content": raw})
                provider.assert_not_called()
            self.assertIn("after", marker)
            self.assertEqual(len(saved_returns), 1)
            self.assertEqual(post_within_update, [])
            # 后续历史/上下文查询是合法生成阶段SQL，不宣称持续排他锁下整个回复必须成功。
            self.assertEqual(frames(response.text)[-1]["type"], "error")
            self.assertEqual(len(statements[marker["after"]:]), 1)
            self.assertIn("LIMIT", statements[marker["after"]])
        finally:
            event.remove(self.sessions.class_, "before_flush", remember)
            event.remove(self.engine, "before_cursor_execute", capture)
            for lock in locks:
                lock.rollback()
                lock.close()
        actual = self.rows()
        self.assertEqual(saved_returns[0], next(row for row in actual["conversation"] if row["id"] == "untitled"))
        user = [row for row in actual["message"] if row["conversation_id"] == "untitled"]
        self.assertEqual(len(user), 1)
        self.assertEqual((user[0]["role"], user[0]["content"]), ("user", raw))

    def test_legacy_normal_first_title_and_full_reply_done_remain_compatible(self):
        raw, reply = "\n　旧协议首原稿😀\0　\n", "受控完整回复😀\0"
        with patch.object(chat.llm, "stream_complete", return_value=iter([reply])) as provider:
            response = self.client.post("/api/v1/conversations/untitled/messages", json={"content": raw})
            provider.assert_called_once()
        events = frames(response.text)
        self.assertEqual([row["type"] for row in events], ["token", "done"])
        self.assertEqual(events[-1]["title"], raw.strip()[:20])
        messages = [row for row in self.rows()["message"] if row["conversation_id"] == "untitled"]
        self.assertEqual([(row["role"], row["content"]) for row in messages], [("user", raw), ("assistant", reply)])

    def test_unknown_columns_twenty_tables_foreign_history_and_schema_are_preserved(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE conversation ADD COLUMN legacy_note TEXT DEFAULT '原私有会话列😀'")
            connection.exec_driver_sql("ALTER TABLE message ADD COLUMN legacy_note TEXT DEFAULT '原消息列😀'")
            connection.exec_driver_sql("UPDATE conversation SET legacy_note='无关会话原值' WHERE id='foreign-thread'")
        before, schema = self.rows(), self.schema()
        self.assertEqual(len(before), 21)
        self.assertIn('outline_append_receipt', before)
        created = self.client.post("/api/v1/conversations", json={"novel_id": None})
        renamed = self.client.patch("/api/v1/conversations/bound", json={"title": self.title})
        moved = self.client.patch("/api/v1/conversations/global", json={"novel_id": "work"})
        self.assertEqual((created.status_code, renamed.status_code, moved.status_code), (201, 200, 200))
        self.assertEqual([len(row.json()) for row in (created, renamed, moved)], [5, 5, 5])
        after = self.rows()
        self.assertEqual(self.schema(), schema)
        self.assertEqual(next(row for row in after["conversation"] if row["id"] == "foreign-thread"),
                         next(row for row in before["conversation"] if row["id"] == "foreign-thread"))
        self.assertEqual({name: value for name, value in after.items() if name != "conversation"},
                         {name: value for name, value in before.items() if name != "conversation"})
