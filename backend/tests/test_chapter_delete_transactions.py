"""永久删除章节：真实 SQLite 提交失败不得污染池中的下一次请求。"""
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.infrastructure import db as db_module
from app.infrastructure import models, repository as repos
from app.interfaces.api.v1 import chapters, novels


class ChapterDeleteTransactionTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "permanent-delete.sqlite"
        self.engine = create_engine(f"sqlite:///{self.path.as_posix()}",
            connect_args={"check_same_thread": False, "timeout": .2})
        self.addCleanup(self.engine.dispose)
        models.Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        self.denials, self.closed, self.sql, self.responses = [], [], [], []
        self.deny_commit, self.exclusive, self.current, self.lock, self.post_start = False, False, None, None, None
        self.completed_commits, self.commit_attempts = 0, 0
        self.provider_attempts = []
        for target in ("app.infrastructure.ai.llm_provider.LLMProvider.complete",
            "app.infrastructure.ai.llm_provider.LLMProvider.stream_complete", "app.infrastructure.ai.embedding.EmbeddingService.embed"):
            mocked = patch(target, side_effect=lambda *args, **kwargs: self.fail("DELETE tests must not call a model or embedding"))
            mocked.start(); self.addCleanup(mocked.stop)
        guarded_sessions = patch.object(db_module, "SessionLocal", self.sessions)
        guarded_sessions.start(); self.addCleanup(guarded_sessions.stop)
        def before_sql(connection, cursor, statement, parameters, *args):
            self.sql.append(statement)
        event.listen(self.engine, "before_cursor_execute", before_sql)
        def before_commit(db):
            self.commit_attempts += 1
        def after_commit(db):
            self.completed_commits += 1
            if self.exclusive and db is self.current:
                self.lock = sqlite3.connect(self.path, timeout=.2, check_same_thread=False)
                self.lock.execute("BEGIN EXCLUSIVE")
                self.post_start = len(self.sql)
        for name, listener in (("before_commit", before_commit), ("after_commit", after_commit)):
            event.listen(self.sessions.class_, name, listener)
            self.addCleanup(event.remove, self.sessions.class_, name, listener)
        def authorizer(action, arg1, arg2, database, context):
            if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT" and self.deny_commit:
                self.deny_commit = False
                self.denials.append(arg1)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self.authorizer = authorizer
        def dependency():
            # Exercise the actual production finally/close, with the real
            # autoflush=False SessionLocal; remove the test authorizer first.
            generator = db_module.get_db()
            db = next(generator)
            self.current = db
            driver = db.connection().connection.driver_connection
            driver.set_authorizer(authorizer)
            try:
                yield db
            finally:
                driver.set_authorizer(None)
                generator.close()
                self.closed.append({"driver_in_transaction": driver.in_transaction,
                    "Session_in_transaction": db.in_transaction()})
                self.current = None
        app = FastAPI()
        app.include_router(chapters.router, prefix="/api/v1")
        app.include_router(novels.router, prefix="/api/v1")
        app.dependency_overrides[db_module.get_db] = dependency
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.addCleanup(self.release_lock)
        with self.sessions() as db:
            db.add_all([models.Novel(id="work", title="作者作品"), models.Novel(id="foreign", title="其它作品"),
                models.Conversation(id="global", novel_id=None, title="全局 NULL 会话😀"),
                models.Chapter(id="sibling", novel_id="work", seq=2, title="复制祖先留存", content="复制旧稿😀\0"),
                models.Chapter(id="foreign-chapter", novel_id="foreign", seq=1, content="外作品原稿🐾\0")])
            db.add(models.Message(id="global-message", conversation_id="global", role="assistant", content="全局原消息😀\0"))
            db.commit()
        self.create_payload = {"request_id": str(uuid.uuid4()), "seq": 1, "title": "初次创建😀",
            "content": "\r\n　初次稿🐈\0　\n", "directive": "原指令\0"}
        created = self.client.post("/api/v1/novels/work/chapters", json=self.create_payload)
        self.assertEqual(created.status_code, 201)
        self.cid = created.json()["id"]
        self.base = f"/api/v1/novels/work/chapters/{self.cid}"
        saved = self.client.put(self.base, json={"title": "当前作者稿😀", "content": "当前正文🐾\0\r\n",
            "directive": "当前指令\0", "revision_source": "manual_save"})
        self.assertEqual(saved.status_code, 200)
        with self.sessions() as db:
            db.add_all([models.Foreshadowing(id="affected", novel_id="work", content="伏笔本身保留🐾\0",
                planted_chapter_id=self.cid, consumed_chapter_id=self.cid),
                models.Foreshadowing(id="other-foreshadow", novel_id="work", content="其它章伏笔保持", planted_chapter_id="sibling")])
            for novel_id, chapter_id in (("work", self.cid), ("work", "sibling"), ("foreign", "foreign-chapter")):
                db.add(models.MemoryChunk(novel_id=novel_id, kind="chapter_slice", source_chapter_id=chapter_id,
                    text="既有记忆🐈\0", embedding_id="preexisting-fixed-vector", vector=[.25]))
                db.add(models.ChapterSettleReceipt(novel_id=novel_id, chapter_id=chapter_id,
                    source_fingerprint="a" * 64, suggestion_fingerprint="b" * 64, kind="character",
                    entity_id="preexisting", payload_json={"name": "原建议🐾\0"}))
            db.add_all([models.MemoryChunk(novel_id="work", kind="setting", embedding_id="affected", text="旧引用索引", vector=[.5]),
                models.MemoryChunk(novel_id="work", kind="setting", embedding_id="other-foreshadow", text="保留索引", vector=[.125]),
                models.ChapterRevision(novel_id="work", chapter_id="sibling", revision_no=1, content="复制祖先旧文😀\0"),
                models.ChapterRevision(novel_id="foreign", chapter_id="foreign-chapter", revision_no=1, content="外作品旧版本🐾\0")])
            db.commit()
        self.before = self.rows()

    def rows(self):
        with closing(sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            self.assertEqual(len(tables), 21)
            self.assertIn('outline_append_receipt', tables)
            self.assertEqual([list(row) for row in db.execute("PRAGMA integrity_check")], [["ok"]])
            return {table: [dict(row) for row in db.execute(f'SELECT * FROM "{table}" ORDER BY rowid')] for table in tables}

    def assert_writer_released(self):
        with closing(sqlite3.connect(self.path, timeout=.2)) as observer:
            observer.execute("BEGIN EXCLUSIVE")
            self.assertTrue(observer.in_transaction)
            observer.rollback()

    def release_lock(self):
        if self.lock is not None:
            self.lock.rollback(); self.lock.close(); self.lock = None

    def delete(self):
        start = len(self.sql)
        response = self.client.delete(self.base)
        self.responses.append({"status": response.status_code, "body": response.text, "SQL": self.sql[start:]})
        return response

    def test_denied_commit_releases_driver_and_cannot_leak_into_later_native_request(self):
        start = self.completed_commits
        self.deny_commit = True
        rejected = self.delete()
        self.assertEqual(rejected.status_code, 500)
        self.assertEqual(self.denials, ["COMMIT"])
        self.assertEqual(self.completed_commits, start)
        for prefix in ("DELETE FROM chapter_settle_receipt", "DELETE FROM chapter_revision", "DELETE FROM memory_chunk", "DELETE FROM chapter WHERE"):
            self.assertTrue(any(statement.startswith(prefix) for statement in self.responses[-1]["SQL"]), prefix)
        self.assertEqual(self.closed[-1], {"driver_in_transaction": False, "Session_in_transaction": False})
        self.assert_writer_released()
        self.assertEqual(self.rows(), self.before)
        self.assertEqual(self.client.get(self.base).status_code, 200)
        start_sql = len(self.sql)
        another = self.client.post("/api/v1/novels", json={"title": "合法另一次作品创建"})
        self.assertEqual(another.status_code, 201)
        self.assertFalse(any(statement.startswith("DELETE") for statement in self.sql[start_sql:]))
        actual = self.rows()
        actual["novel"] = [row for row in actual["novel"] if row["id"] != another.json()["id"]]
        self.assertEqual(actual, self.before)
        self.assertEqual(self.client.get(self.base).status_code, 200)

    def test_final_delete_flush_abort_rolls_back_preceding_cleanup(self):
        ddl = f"CREATE TRIGGER reject_final_delete BEFORE DELETE ON chapter WHEN OLD.id='{self.cid}' BEGIN SELECT RAISE(ABORT,'controlled chapter DELETE flush failure'); END"
        with self.engine.begin() as connection:
            connection.exec_driver_sql(ddl)
        try:
            commits = self.completed_commits
            rejected = self.delete()
            self.assertEqual(rejected.status_code, 500)
            self.assertEqual(self.completed_commits, commits)
            self.assertEqual(self.denials, [])
            self.assertTrue(any(statement.startswith("DELETE FROM chapter WHERE") for statement in self.responses[-1]["SQL"]))
            self.assertEqual(self.rows(), self.before)
            self.assertEqual(self.closed[-1]["driver_in_transaction"], False)
            self.assert_writer_released()
        finally:
            with self.engine.begin() as connection:
                connection.exec_driver_sql("DROP TRIGGER reject_final_delete")

    def test_success_204_cleanup_tombstone_and_postcommit_response_require_no_SQL(self):
        commits, attempts = self.completed_commits, self.commit_attempts
        self.exclusive = True
        deleted = self.delete()
        try:
            self.assertEqual((deleted.status_code, deleted.content), (204, b""))
            self.assertEqual(self.completed_commits - commits, 1)
            self.assertEqual(self.commit_attempts - attempts, 1)
            self.assertIsNotNone(self.lock)
            self.assertTrue(self.lock.in_transaction)
            self.assertEqual(self.sql[self.post_start:], [])
        finally:
            self.release_lock()
            self.exclusive = False
        after = self.rows()
        changed = {"chapter", "chapter_revision", "chapter_settle_receipt", "memory_chunk", "foreshadowing"}
        for table in set(self.before) - changed:
            self.assertEqual(after[table], self.before[table], table)
        for table in ("chapter_revision", "chapter_settle_receipt"):
            self.assertEqual(after[table], [row for row in self.before[table] if row["chapter_id"] != self.cid])
        self.assertEqual(after["chapter"], [row for row in self.before["chapter"] if row["id"] != self.cid])
        self.assertEqual(after["memory_chunk"], [row for row in self.before["memory_chunk"]
            if row["source_chapter_id"] != self.cid and row["embedding_id"] != "affected"])
        for row in after["foreshadowing"]:
            old = next(item for item in self.before["foreshadowing"] if item["id"] == row["id"])
            if row["id"] == "affected":
                self.assertEqual((row["planted_chapter_id"], row["consumed_chapter_id"]), ("", ""))
                for field in set(old) - {"planted_chapter_id", "consumed_chapter_id", "updated_at"}:
                    self.assertEqual(row[field], old[field])
            else:
                self.assertEqual(row, old)
        self.assertEqual(self.client.get(self.base).status_code, 404)
        self.assertEqual(self.client.get(self.base + "/revisions").status_code, 404)
        self.assertEqual(self.delete().status_code, 404)
        replay = self.client.post("/api/v1/novels/work/chapters", json=self.create_payload)
        self.assertEqual((replay.status_code, replay.json()["code"]), (409, "chapter_creation_deleted"))
        self.assertEqual(self.rows(), after)

    def test_404_preserves_caller_pending_and_success_still_commits_it_together(self):
        with self.sessions() as db:
            author = db.get(models.Novel, "work")
            author.summary = "调用方有意未提交梗概😀\0"
            extra = models.Novel(title="调用方有意未提交新作品")
            db.add(extra)
            before_sql = len(self.sql)
            with self.assertRaises(HTTPException) as error:
                repos.ChapterRepo(db).delete("work", "foreign-chapter")
            self.assertEqual(error.exception.status_code, 404)
            self.assertIn(author, db.dirty)
            self.assertIn(extra, db.new)
            self.assertTrue(db.in_transaction())
            self.assertFalse(any(statement.startswith(("UPDATE", "INSERT", "DELETE")) for statement in self.sql[before_sql:]))
            self.assertEqual(self.rows(), self.before)
            commits = self.completed_commits
            repos.ChapterRepo(db).delete("work", self.cid)
            self.assertEqual(self.completed_commits - commits, 1)
        actual = self.rows()
        self.assertEqual(next(row for row in actual["novel"] if row["id"] == "work")["summary"], "调用方有意未提交梗概😀\0")
        self.assertTrue(any(row["title"] == "调用方有意未提交新作品" for row in actual["novel"]))
        self.assertFalse(any(row["id"] == self.cid for row in actual["chapter"]))
        self.assertTrue(any(row["id"] == "foreign-chapter" for row in actual["chapter"]))

    def test_direct_caller_commit_denial_returns_an_active_rolled_back_session(self):
        with self.sessions() as db:
            driver = db.connection().connection.driver_connection
            driver.set_authorizer(self.authorizer)
            self.deny_commit = True
            try:
                with self.assertRaises(Exception) as error:
                    repos.ChapterRepo(db).delete("work", self.cid)
                self.assertIn("not authorized", str(error.exception))
                self.assertEqual(self.denials, ["COMMIT"])
                self.assertTrue(db.is_active)
                self.assertFalse(db.in_transaction())
                self.assertFalse(driver.in_transaction)
                self.assert_writer_released()
                self.assertEqual(self.rows(), self.before)
            finally:
                driver.set_authorizer(None)
                db.rollback()


if __name__ == "__main__":
    unittest.main()
