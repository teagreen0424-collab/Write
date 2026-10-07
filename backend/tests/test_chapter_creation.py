"""手工新章真实SQLite边界：请求重放、并发序号、墓碑与提交后的响应。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from app.application import auto_pilot, chapter_creation
from app.infrastructure import db as database, models, repository as repos
from app.infrastructure.db import get_db
from app.interfaces import schemas
from app.interfaces.api.v1 import chapters


class ChapterCreationTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory(prefix="lingmo-creation-")
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "creation.db"
        self.engine = create_engine(f"sqlite:///{self.path.as_posix()}",
                                   connect_args={"check_same_thread": False, "timeout": .2})
        self.addCleanup(self.engine.dispose)
        models.Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        self.nid = repos.NovelRepo(self.db).create(title="创建验收").id
        self.other = repos.NovelRepo(self.db).create(title="另一作品").id
        self.app = FastAPI()
        self.app.include_router(chapters.router, prefix="/api/v1")

        def isolated_db():
            with self.Session() as db:
                yield db

        self.app.dependency_overrides[get_db] = isolated_db
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.base = f"/api/v1/novels/{self.nid}/chapters"
        self.key = str(uuid4())
        self.payload = {"request_id": self.key, "title": "初稿", "content": "甲 乙😀", "directive": "保留原稿"}
        for target in ("httpx.HTTPTransport.handle_request", "httpx.AsyncHTTPTransport.handle_async_request",
                       "app.infrastructure.ai.embedding.EmbeddingService.embed",
                       "app.infrastructure.ai.llm_provider.LLMProvider.complete",
                       "app.infrastructure.ai.llm_provider.LLMProvider.stream_complete"):
            guard = patch(target, side_effect=AssertionError("创建/重放不能调用外部服务"))
            guard.start()
            self.addCleanup(guard.stop)

    def post(self, payload=None, novel_id=None):
        return self.client.post(f"/api/v1/novels/{novel_id or self.nid}/chapters",
                                json=self.payload if payload is None else payload)

    def rows(self, model):
        with self.Session() as db:
            return [dict(row) for row in db.execute(select(model.__table__)).mappings()]

    def first(self, payload=None):
        response = self.post(payload)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertFalse(response.json()["creation_replayed"])
        self.assertFalse(response.json()["author_changed_since_creation"])
        return response.json()

    def assert_conflict(self, response, code):
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["code"], code)

    def test_response_loss_retry_same_uuid_and_defaults_returns_one_chapter(self):
        created = self.first()
        retry = self.post({"outline_node_id": "", "seq": None, "status": "draft",
                           **self.payload, "request_id": self.key.upper(), "ignored": "无效字段不参与"})
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json(), {**created, "creation_replayed": True})
        self.assertEqual(len(self.rows(models.Chapter)), 1)
        receipt, = self.rows(models.ChapterCreationReceipt)
        self.assertEqual((receipt["request_id"], receipt["chapter_id"]), (self.key, created["id"]))

    def test_changed_effective_payload_never_overwrites_existing_chapter(self):
        original = self.first()
        before = self.rows(models.Chapter)
        for field, value in (("title", "新标题"), ("content", "新稿"), ("directive", "新要求"),
                             ("status", "finalized"), ("seq", 1), ("outline_node_id", "已不存在")):
            with self.subTest(field=field):
                response = self.post({**self.payload, field: value})
                self.assert_conflict(response, "chapter_creation_payload_conflict")
                self.assertEqual(self.rows(models.Chapter), before)
        self.assertEqual(original["seq"], 1)

    def test_same_key_cross_scope_rejected_without_foreign_body(self):
        self.first()
        response = self.post(novel_id=self.other)
        self.assert_conflict(response, "chapter_creation_scope_conflict")
        self.assertNotIn("content", response.json())
        self.assertEqual(len(self.rows(models.Chapter)), 1)
        self.assertEqual(self.post(novel_id="unknown").status_code, 404)

    def test_invalid_uuid_is_422_and_legacy_identical_creations_stay_legal(self):
        for value in ("bad-uuid", "", False, 7):
            with self.subTest(value=value):
                self.assertEqual(self.post({**self.payload, "request_id": value}).status_code, 422)
        legacy = {key: value for key, value in self.payload.items() if key != "request_id"}
        first, second = self.first(legacy), self.first(legacy)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual((first["seq"], second["seq"]), (1, 2))
        self.assertEqual(self.rows(models.ChapterCreationReceipt), [])

    def test_empty_author_defaults_are_not_automatically_numbered_or_marked_changed(self):
        created = self.first({"request_id": self.key})
        self.assertEqual((created["title"], created["content"], created["directive"]), ("", "", ""))
        replay = self.post({"request_id": self.key}).json()
        self.assertFalse(replay["author_changed_since_creation"])

    def test_replay_precedes_mutable_outline_validation_but_new_requests_are_validated(self):
        node = repos.OutlineRepo(self.db).create(self.nid, title="绑定章", level="chapter")
        nid = node.id
        payload = {**self.payload, "outline_node_id": nid}
        created = self.first(payload)
        repos.OutlineRepo(self.db).delete(self.nid, nid)
        replay = self.post(payload)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["id"], created["id"])
        response = self.post({**payload, "request_id": str(uuid4())})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.rows(models.ChapterCreationReceipt)), 1)
        foreign = repos.OutlineRepo(self.db).create(self.other, title="异作品章", level="chapter").id
        volume = repos.OutlineRepo(self.db).create(self.nid, title="卷", level="volume").id
        for binding in (foreign, volume, "missing"):
            self.assertEqual(self.post({"request_id": str(uuid4()), "outline_node_id": binding}).status_code, 400)

    def test_replay_is_readonly_and_returns_current_author_and_derived_columns(self):
        created = self.first()
        # 留下旧身份缓存后另一真实接口修改，重放不能拿缓存的创建快照。
        cached = repos.ChapterRepo(self.db).get(self.nid, created["id"])
        edited = self.client.put(self.base + "/" + created["id"], json={"content": "新的作者稿甲😀"})
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(cached.content, self.payload["content"])
        with self.Session() as db:
            row = repos.ChapterRepo(db).get(self.nid, created["id"])
            row.status = "finalized"; row.summary = "新稿摘要"; row.tension_score = 7.5; row.drift_score = .2
            db.add(models.MemoryChunk(novel_id=self.nid, source_chapter_id=row.id, text="新记忆", vector=[1.0]))
            db.commit()
        before = {model: self.rows(model) for model in (models.Chapter, models.ChapterCreationReceipt,
                  models.ChapterRevision, models.MemoryChunk)}
        statements = []
        listener = lambda _conn, _cursor, statement, *_: statements.append(statement)
        event.listen(self.engine, "before_cursor_execute", listener)
        try:
            replay = chapter_creation.create_manual_chapter(self.db, self.nid, schemas.ChapterCreate(**self.payload))
        finally:
            event.remove(self.engine, "before_cursor_execute", listener)
        self.assertEqual(replay["content"], "新的作者稿甲😀")
        self.assertTrue(replay["author_changed_since_creation"])
        self.assertEqual((replay["status"], replay["summary"], replay["tension_score"], replay["drift_score"]),
                         ("finalized", "新稿摘要", 7.5, .2))
        self.assertTrue(all(sql.lstrip().upper().startswith("SELECT") for sql in statements), statements)
        self.assertEqual(before, {model: self.rows(model) for model in before})
        meta = self.client.get(self.base + "/metadata").json()[0]
        self.assertEqual(meta["word_count"], 7)

    def test_status_summary_only_changes_do_not_claim_author_changed(self):
        created = self.first()
        response = self.client.put(self.base + "/" + created["id"], json={"status": "finalized", "summary": "摘要"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.post().json()["author_changed_since_creation"])

    def test_deleted_chapter_keeps_receipt_and_late_retry_cannot_resurrect(self):
        created = self.first()
        self.assertEqual(self.client.delete(self.base + "/" + created["id"]).status_code, 204)
        self.assert_conflict(self.post(), "chapter_creation_deleted")
        self.assertEqual(self.rows(models.Chapter), [])
        self.assertEqual(len(self.rows(models.ChapterCreationReceipt)), 1)
        self.first({**self.payload, "request_id": str(uuid4())})

    def test_deleting_novel_cleans_only_its_creation_receipts(self):
        self.first()
        foreign_payload = {**self.payload, "request_id": str(uuid4())}
        self.assertEqual(self.post(foreign_payload, novel_id=self.other).status_code, 201)
        repos.NovelRepo(self.db).delete(self.nid)
        receipt, = self.rows(models.ChapterCreationReceipt)
        chapter, = self.rows(models.Chapter)
        self.assertEqual((receipt["novel_id"], chapter["novel_id"]), (self.other, self.other))
        self.assertEqual(self.post().status_code, 404)

    def _concurrent(self, payloads):
        gate = Barrier(len(payloads))
        read_gate = Barrier(len(payloads))
        connections = []
        begin_write = chapter_creation._begin_chapter_write

        def begin_after_both_reads(db):
            connections.append(id(db.connection().connection.driver_connection))
            read_gate.wait(timeout=5)
            begin_write(db)

        def submit(payload):
            gate.wait(timeout=5)
            with self.Session() as db:
                return chapter_creation.create_manual_chapter(db, self.nid, schemas.ChapterCreate(**payload))

        with patch.object(chapter_creation, "_begin_chapter_write", side_effect=begin_after_both_reads), \
                ThreadPoolExecutor(max_workers=len(payloads)) as executor:
            results = list(executor.map(submit, payloads))
        self.assertEqual(len(set(connections)), len(payloads))
        return results

    def test_two_physical_connections_same_key_create_once(self):
        results = self._concurrent([self.payload, self.payload])
        self.assertEqual(len({row["id"] for row in results}), 1)
        self.assertEqual(sorted(row["creation_replayed"] for row in results), [False, True])
        self.assertEqual(len(self.rows(models.Chapter)), 1)
        self.assertEqual(len(self.rows(models.ChapterCreationReceipt)), 1)

    def test_parallel_distinct_keys_and_legacy_default_sequences_are_allocated_under_lock(self):
        keyed = [self.payload, {**self.payload, "request_id": str(uuid4())}]
        results = self._concurrent(keyed)
        self.assertEqual(sorted(row["seq"] for row in results), [1, 2])
        legacy = {key: value for key, value in self.payload.items() if key != "request_id"}
        results = self._concurrent([legacy, legacy])
        self.assertEqual(sorted(row["seq"] for row in results), [3, 4])

    def test_receipt_trigger_failure_rolls_back_chapter_and_auto_sequence(self):
        with self.engine.begin() as connection:
            connection.execute(text("CREATE TRIGGER receipt_fail BEFORE INSERT ON chapter_creation_receipt "
                                    "BEGIN SELECT RAISE(ABORT, 'receipt rejected'); END"))
        self.assertEqual(self.post().status_code, 500)
        self.assertEqual(self.rows(models.Chapter), [])
        self.assertEqual(self.rows(models.ChapterCreationReceipt), [])
        with self.engine.begin() as connection:
            connection.execute(text("DROP TRIGGER receipt_fail"))
        self.assertEqual(self.first()["seq"], 1)

    def test_postcommit_exclusive_lock_does_not_negate_http_saved_response(self):
        blocker = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.addCleanup(blocker.close)
        committed, statements = [], []
        original_commit = self.db.commit

        def commit_then_lock():
            original_commit()
            committed.append(True)
            blocker.execute("BEGIN EXCLUSIVE")

        def listener(_conn, _cursor, statement, *_):
            if committed:
                statements.append(statement)

        self.app.dependency_overrides[get_db] = lambda: self.db
        event.listen(self.engine, "before_cursor_execute", listener)
        try:
            with patch.object(self.db, "commit", side_effect=commit_then_lock):
                saved = self.first()
            self.assertEqual(statements, [])
        finally:
            blocker.execute("ROLLBACK")
            event.remove(self.engine, "before_cursor_execute", listener)
        self.assertEqual(self.rows(models.Chapter)[0]["id"], saved["id"])
        self.assertEqual(self.post().status_code, 200)

    def test_real_write_lock_returns_honest_unknown_outcome_then_original_retry_succeeds(self):
        blocker = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            response = self.post()
            self.assert_conflict(response, "chapter_creation_busy")
            self.assertIn("尚未确认", response.json()["detail"])
        finally:
            blocker.execute("ROLLBACK")
        self.assertEqual(self.first()["seq"], 1)

    def test_shared_repo_freezes_same_identity_and_preserves_autopilot_core_sorting(self):
        nodes = [repos.OutlineRepo(self.db).create(self.nid, title=f"章{i}", sort_order=i).id for i in range(3)]
        before = repos.ChapterRepo(self.db).create(self.nid, seq=1, outline_node_id=nodes[0], content="原稿一")
        after = repos.ChapterRepo(self.db).create(self.nid, seq=2, outline_node_id=nodes[2], content="原稿三",
                                                    summary="保留摘要", status="finalized")
        prior_updated = after.updated_at
        seq = auto_pilot._chapter_insert_seq(self.db, self.nid, nodes[1], nodes)
        blocker = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(blocker.close)
        original_commit = self.db.commit

        def commit_then_lock():
            original_commit()
            blocker.execute("BEGIN EXCLUSIVE")

        try:
            with patch.object(self.db, "commit", side_effect=commit_then_lock):
                inserted = repos.ChapterRepo(self.db).create(self.nid, seq=seq, outline_node_id=nodes[1], content="补章")
            self.assertIs(self.db.identity_map.get((models.Chapter, (inserted.id,), None)), inserted)
            self.assertEqual(schemas.ChapterRead.model_validate(inserted).content, "补章")
            self.assertFalse(self.db.dirty)
        finally:
            blocker.execute("ROLLBACK")
        rows = {row["id"]: row for row in self.rows(models.Chapter)}
        self.assertEqual((rows[before.id]["seq"], rows[inserted.id]["seq"]), (1, 2))
        self.assertEqual((rows[after.id]["seq"], rows[after.id]["content"], rows[after.id]["summary"], rows[after.id]["updated_at"]),
                         (3, "原稿三", "保留摘要", prior_updated))

    def test_shared_commit_false_preserves_caller_pending_orm_and_core_until_rollback(self):
        self.db.add(models.Character(novel_id=self.nid, name="调用方未提交人物"))
        self.db.execute(text("UPDATE novel SET summary='调用方未提交摘要' WHERE id=:id"), {"id": self.nid})
        chapter = repos.ChapterRepo(self.db).create(self.nid, commit=False, content="未提交新稿")
        self.assertTrue(chapter.id)
        self.assertEqual(self.rows(models.Chapter), [])
        self.assertEqual(self.rows(models.Character), [])
        self.db.rollback()
        self.assertEqual(self.rows(models.Chapter), [])
        self.assertEqual(next(row["summary"] for row in self.rows(models.Novel) if row["id"] == self.nid), "")

    def test_manual_guard_rejects_flushed_and_core_caller_writes_without_rollback(self):
        for mode in ("flushed", "core"):
            with self.subTest(mode=mode):
                with self.Session() as db:
                    if mode == "flushed":
                        db.add(models.Character(novel_id=self.nid, name="已flush但未提交")); db.flush()
                    else:
                        db.execute(text("UPDATE novel SET summary='Core未提交' WHERE id=:id"), {"id": self.nid})
                    with self.assertRaises(RuntimeError), patch.object(db, "rollback", wraps=db.rollback) as rollback:
                        chapter_creation.create_manual_chapter(db, self.nid, schemas.ChapterCreate(**self.payload))
                    rollback.assert_not_called()
                    db.commit()
        self.assertEqual(len(self.rows(models.Character)), 1)
        self.assertEqual(next(row["summary"] for row in self.rows(models.Novel) if row["id"] == self.nid), "Core未提交")
        self.assertEqual(self.rows(models.Chapter), [])

    def test_old_database_init_twice_preserves_rows_without_receipt_backfill(self):
        created = repos.ChapterRepo(self.db).create(self.nid, title="旧稿", content="作者旧稿")
        before = self.rows(models.Chapter)
        with self.engine.begin() as connection:
            connection.execute(text("DROP TABLE chapter_creation_receipt"))
        with patch.object(database, "engine", self.engine):
            database.init_db(); database.init_db()
        self.assertEqual(self.rows(models.Chapter), before)
        self.assertEqual(self.rows(models.ChapterCreationReceipt), [])
        result = self.first()
        self.assertNotEqual(result["id"], created.id)
        with patch.object(database, "engine", self.engine):
            database.init_db(); database.init_db()
        self.assertEqual(len(self.rows(models.ChapterCreationReceipt)), 1)


if __name__ == "__main__":
    unittest.main()
