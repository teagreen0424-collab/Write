"""普通作品保存：真实提交后的锁不能否认保存，共享调用方事务合同不变。"""
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

from app.infrastructure import models, repository as repos
from app.infrastructure.db import get_db
from app.interfaces.api.v1 import novels


class NovelCommitResponseTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "作品保存真事务.sqlite"
        self.engine = create_engine(f"sqlite:///{self.path.as_posix()}",
            connect_args={"check_same_thread": False, "timeout": .03})
        self.addCleanup(self.engine.dispose)
        models.Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=True)
        with self.sessions() as db:
            db.add_all([models.Novel(id="work", title="作者作品", genre="原题材", summary="原梗概", style_anchor="原样式锚"),
                models.Novel(id="foreign", title="无关作品", style_anchor="无关文风")])
            db.add_all([models.Chapter(id="prior", novel_id="work", seq=1, title="调用方原章", content="原作者稿😀\0"),
                models.Chapter(id="foreign-chapter", novel_id="foreign", seq=1, title="其它作品原章", content="不可改动😀\0")])
            db.add(models.Conversation(id="thread", novel_id="work", title="原会话"))
            db.add(models.Message(id="message", conversation_id="thread", role="assistant", content="原消息😀\0", meta={"原": True}))
            db.commit()
        self.app = FastAPI()
        self.app.include_router(novels.router, prefix="/api/v1")

        def dependency():
            with self.sessions() as db:
                yield db

        self.app.dependency_overrides[get_db] = dependency
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.payload = {"title": "实际保存作品😀", "genre": "新题材", "summary": "\n　精确作者梗概😀\0　\n"}

    def rows(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {name: [dict(row) for row in db.execute(f'SELECT * FROM "{name}" ORDER BY rowid')] for name in names}

    def schema(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()

    def actual_postcommit_exclusive(self, mode):
        before, statements, blockers, marker = self.rows(), [], [], {}
        original_commit = self.sessions.class_.commit

        def remember(db, *_args):
            if any(isinstance(obj, models.Novel) and obj.title == self.payload["title"] for obj in (*db.new, *db.dirty)):
                db.info["novel-target"] = True

        def commit(db):
            original_commit(db)  # 原事务真正完成以后，才让独立物理连接持有排他锁。
            if db.info.pop("novel-target", False):
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
                response = (self.client.post("/api/v1/novels", json=self.payload) if mode == "create"
                    else self.client.put("/api/v1/novels/work", json=self.payload))
            self.assertIn("after", marker)
            self.assertEqual(response.status_code, 201 if mode == "create" else 200, response.text)
            self.assertEqual(statements[marker["after"]:], [])
            body = response.json()
            self.assertEqual(set(body), {column.key for column in models.Novel.__table__.columns})
            self.assertEqual(len(body), 7)
            self.assertTrue(all(body[key] == value for key, value in self.payload.items()))
        finally:
            event.remove(self.sessions.class_, "before_flush", remember)
            event.remove(self.engine, "before_cursor_execute", capture)
            for blocker in blockers:
                blocker.rollback()
                blocker.close()
        after = self.rows()
        self.assertEqual(next(row for row in after["novel"] if row["id"] == body["id"]), body)
        self.assertEqual(len(after["novel"]), len(before["novel"]) + (mode == "create"))
        self.assertEqual(next(row for row in after["novel"] if row["id"] == "foreign"),
                         next(row for row in before["novel"] if row["id"] == "foreign"))
        self.assertEqual({key: value for key, value in after.items() if key != "novel"},
                         {key: value for key, value in before.items() if key != "novel"})

    def test_native_create_real_postcommit_exclusive_returns201_all_columns_zero_sql(self):
        self.actual_postcommit_exclusive("create")

    def test_native_update_real_postcommit_exclusive_returns200_all_columns_zero_sql(self):
        self.actual_postcommit_exclusive("update")

    def shared_caller_states(self, mode):
        for kind in ("new", "dirty", "deleted", "flushed", "core"):
            with self.subTest(kind=kind), self.sessions() as db:
                row_id = f"caller-{mode}-{kind}"
                caller = models.Chapter(id=row_id, novel_id="work", seq=2, title="调用方原稿", content="保留正文😀\0")
                db.add(caller)
                db.commit()
                target = db.get(models.Novel, "work")
                before = self.rows()
                if kind == "new":
                    db.add(models.Character(id=row_id, novel_id="work", name="调用方新增人物"))
                elif kind == "deleted":
                    db.delete(caller)
                elif kind == "core":
                    db.execute(text("UPDATE chapter SET title=:title WHERE id=:id"), {"title": "调用方Core稿", "id": row_id})
                else:
                    caller.title = "调用方未提交稿"
                    if kind == "flushed":
                        db.flush()
                added, commits, post_sql = [], [], []
                original_commit = db.commit

                def remember(_db, *_args):
                    added.extend(obj for obj in db.new if isinstance(obj, models.Novel))

                def commit():
                    original_commit()
                    commits.append(True)

                def capture(_connection, _cursor, statement, *_args):
                    if commits:
                        post_sql.append(statement)

                event.listen(db, "before_flush", remember)
                event.listen(self.engine, "before_cursor_execute", capture)
                try:
                    with patch.object(db, "commit", side_effect=commit) as commit_call, patch.object(db, "rollback", wraps=db.rollback) as rollback:
                        saved = (repos.NovelRepo(db).create(**self.payload) if mode == "create"
                            else repos.NovelRepo(db).update("work", **self.payload))
                        commit_call.assert_called_once_with()
                        rollback.assert_not_called()
                    self.assertIs(saved, added[-1] if mode == "create" else target)
                    self.assertFalse(inspect(saved).expired_attributes)
                    self.assertEqual({column.key: getattr(saved, column.key) for column in saved.__table__.columns}["summary"], self.payload["summary"])
                    self.assertFalse(db.new or db.dirty or db.deleted)
                    self.assertEqual(post_sql, [])
                finally:
                    event.remove(db, "before_flush", remember)
                    event.remove(self.engine, "before_cursor_execute", capture)
                after = self.rows()
                if kind == "new":
                    self.assertTrue(any(row["id"] == row_id for row in after["character"]))
                elif kind == "deleted":
                    self.assertFalse(any(row["id"] == row_id for row in after["chapter"]))
                else:
                    self.assertEqual(next(row for row in after["chapter"] if row["id"] == row_id)["title"],
                                     "调用方Core稿" if kind == "core" else "调用方未提交稿")
                self.assertEqual(next(row for row in after["novel"] if row["id"] == "foreign"),
                                 next(row for row in before["novel"] if row["id"] == "foreign"))
                self.assertEqual({key: value for key, value in after.items() if key not in ("novel", "chapter", "character")},
                                 {key: value for key, value in before.items() if key not in ("novel", "chapter", "character")})

    def test_shared_create_commits_all_five_caller_states_same_identity_without_reset(self):
        self.shared_caller_states("create")

    def test_shared_update_commits_all_five_caller_states_same_identity_without_reset(self):
        self.shared_caller_states("update")

    def test_default_query_autoflush_and_noauto_query_order_both_commit_caller(self):
        for mode in ("default", "noauto", "autoflush_false"):
            with self.subTest(mode=mode), self.sessions(autoflush=mode != "autoflush_false") as db:
                caller = db.get(models.Chapter, "prior")
                caller.title = f"作者真实调用方{mode}"
                statements = []

                def capture(_connection, _cursor, statement, *_args):
                    statements.append(statement)

                event.listen(self.engine, "before_cursor_execute", capture)
                try:
                    with db.no_autoflush if mode == "noauto" else nullcontext():
                        saved = repos.NovelRepo(db).update("work", summary=f"新作品梗概{mode}")
                    self.assertFalse(inspect(saved).expired_attributes)
                    self.assertFalse(db.dirty)
                finally:
                    event.remove(self.engine, "before_cursor_execute", capture)
                query = next(i for i, statement in enumerate(statements) if statement.startswith("SELECT novel."))
                write = next(i for i, statement in enumerate(statements) if statement.startswith("UPDATE chapter"))
                self.assertLess(write, query) if mode == "default" else self.assertLess(query, write)
                self.assertEqual(next(row for row in self.rows()["chapter"] if row["id"] == "prior")["title"], caller.title)

    def test_none_ignored_empty_clear_and_exact_style_unicode_default_response(self):
        with self.sessions() as db:
            target = db.get(models.Novel, "work")
            old = {column.key: getattr(target, column.key) for column in target.__table__.columns}
            saved = repos.NovelRepo(db).update("work", title=None, genre=None, summary="", style_anchor="\n　新文风😀\0　\n")
            self.assertIs(saved, target)
            self.assertEqual((saved.title, saved.genre, saved.summary, saved.style_anchor),
                             (old["title"], old["genre"], "", "\n　新文风😀\0　\n"))
            self.assertEqual(saved.created_at, old["created_at"])
            self.assertGreaterEqual(saved.updated_at, old["updated_at"])
            self.assertFalse(inspect(saved).expired_attributes or db.dirty)
            with db.no_autoflush:
                created = repos.NovelRepo(db).create(title="只给标题😀\0")
            self.assertEqual((created.genre, created.summary, created.style_anchor), ("", "", ""))
            self.assertTrue(created.id and created.created_at and created.updated_at)
            self.assertFalse(inspect(created).expired_attributes or db.dirty)
        response = self.client.put("/api/v1/novels/work", json={"title": None, "style_anchor": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], old["title"])
        self.assertEqual(response.json()["style_anchor"], "")

    def test_cached_same_target_core_and_after_insert_trigger_return_actual_columns(self):
        with self.sessions(autoflush=False) as db:
            target = db.get(models.Novel, "work")
            old_title = target.title
            db.execute(text("UPDATE novel SET title=:title,genre=:genre,style_anchor=:style WHERE id='work'"),
                       {"title": "同目标Core作者名😀\0", "genre": "同目标Core题材", "style": "同目标Core样式😀\0"})
            self.assertEqual(target.title, old_title)  # 真实缓存仍是旧值，不能冒充数据库已有事实。
            db.add(models.Character(id="same-target-pending", novel_id="work", name="同次调用方人物"))
            original_commit, marker, post_sql = db.commit, [], []

            def commit():
                original_commit()
                marker.append(True)

            def capture(_connection, _cursor, statement, *_args):
                if marker:
                    post_sql.append(statement)

            event.listen(self.engine, "before_cursor_execute", capture)
            try:
                with db.no_autoflush, patch.object(db, "commit", side_effect=commit) as committed:
                    result = repos.NovelRepo(db).update("work", summary="本次明确摘要😀\0")
                    committed.assert_called_once_with()
                self.assertIs(result, target)
                returned = {column.key: getattr(result, column.key) for column in result.__table__.columns}
                self.assertEqual(post_sql, [])
                self.assertFalse(inspect(result).expired_attributes or db.dirty)
            finally:
                event.remove(self.engine, "before_cursor_execute", capture)
        actual = self.rows()
        self.assertEqual(returned, next(row for row in actual["novel"] if row["id"] == "work"))
        self.assertEqual(returned["title"], "同目标Core作者名😀\0")
        self.assertTrue(any(row["id"] == "same-target-pending" for row in actual["character"]))
        # 真SQLite触发器更改已插入列；响应冻结必须读取提交前的真实七列。
        with self.engine.begin() as connection:
            connection.exec_driver_sql("CREATE TRIGGER normalized_novel AFTER INSERT ON novel BEGIN "
                "UPDATE novel SET title=title || '（存储后的名）',summary=summary || char(0) || '😀' WHERE id=NEW.id; END")
        response = self.client.post("/api/v1/novels", json=self.payload)
        self.assertEqual(response.status_code, 201)
        created = response.json()
        self.assertEqual(created, next(row for row in self.rows()["novel"] if row["id"] == created["id"]))
        self.assertEqual(created["title"], self.payload["title"] + "（存储后的名）")
        self.assertEqual(created["summary"], self.payload["summary"] + "\0😀")

    def test_true_insert_update_trigger_failures_leave_all_caller_writes_uncommitted(self):
        for mode in ("create", "update"):
            with self.subTest(mode=mode):
                with self.engine.begin() as connection:
                    operation = "INSERT" if mode == "create" else "UPDATE"
                    connection.exec_driver_sql(f"CREATE TRIGGER refuse_novel BEFORE {operation} ON novel BEGIN SELECT RAISE(ABORT,'真实作品写入拒绝'); END")
                before = self.rows()
                try:
                    with self.sessions() as db:
                        caller = db.get(models.Chapter, "prior")
                        caller.title = "调用方挂起作者稿"
                        db.add(models.Character(id="pending", novel_id="work", name="挂起人物"))
                        db.execute(text("UPDATE chapter SET title='调用方Core挂起' WHERE id='foreign-chapter'"))
                        with self.assertRaises(SQLAlchemyError):
                            if mode == "create":
                                repos.NovelRepo(db).create(**self.payload)
                            else:
                                repos.NovelRepo(db).update("work", **self.payload)
                        # 仓储不新增自动rollback/catch；原调用方仍负责异常事务的回滚。
                        self.assertFalse(db.is_active)
                        self.assertEqual(self.rows(), before)
                        db.rollback()
                    response = (self.client.post("/api/v1/novels", json=self.payload) if mode == "create"
                        else self.client.put("/api/v1/novels/work", json=self.payload))
                    self.assertEqual(response.status_code, 500)
                    self.assertEqual(self.rows(), before)
                finally:
                    with self.engine.begin() as connection:
                        connection.exec_driver_sql("DROP TRIGGER refuse_novel")

    def test_twenty_tables_unknown_columns_foreign_rows_and_schema_untouched(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE novel ADD COLUMN legacy_note TEXT DEFAULT '本地旧字段😀'")
            connection.exec_driver_sql("ALTER TABLE conversation ADD COLUMN legacy_note TEXT DEFAULT '保留会话旧列'")
            connection.exec_driver_sql("UPDATE novel SET legacy_note='其它作品私有旧值' WHERE id='foreign'")
        before, schema = self.rows(), self.schema()
        self.assertEqual(len(before), 20)
        created = self.client.post("/api/v1/novels", json=self.payload)
        updated = self.client.put("/api/v1/novels/work", json={"summary": self.payload["summary"], "style_anchor": "样式😀\0"})
        self.assertEqual((created.status_code, updated.status_code), (201, 200))
        self.assertEqual(len(created.json()), 7)
        self.assertEqual(len(updated.json()), 7)
        after = self.rows()
        self.assertEqual(self.schema(), schema)
        self.assertEqual(next(row for row in after["novel"] if row["id"] == "foreign"),
                         next(row for row in before["novel"] if row["id"] == "foreign"))
        self.assertEqual(next(row for row in after["novel"] if row["id"] == "work")["legacy_note"],
                         next(row for row in before["novel"] if row["id"] == "work")["legacy_note"])
        self.assertEqual(next(row for row in after["novel"] if row["id"] == created.json()["id"])["legacy_note"], "本地旧字段😀")
        self.assertEqual({key: value for key, value in after.items() if key != "novel"},
                         {key: value for key, value in before.items() if key != "novel"})
