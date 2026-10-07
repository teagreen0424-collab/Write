"""定稿建议：原稿门禁、逐项原子记录、响应丢失重试和生命周期。"""
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier, Thread
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.application import setting_index, settle
from app.application.memory import MemoryWriteResult
from app.application.settlement_receipts import chapter_source_fingerprint
from app.infrastructure import db as database, models, repository as repos
from app.infrastructure.db import get_db
from app.interfaces.api.v1 import chapters


class SettlementReceiptTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="lingmo-settlement-receipts-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "receipts.sqlite"
        self.engine = create_engine(f"sqlite:///{self.path.as_posix()}", connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        models.Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        self.nid = repos.NovelRepo(self.db).create(title="当前作品").id
        self.other = repos.NovelRepo(self.db).create(title="另一作品").id
        self.cid = repos.ChapterRepo(self.db).create(
            self.nid, title="第一章", content="林舟走进灯塔。", directive="灯号必须出现。",
        ).id
        self.base = self.fingerprint()
        embedding = patch.object(setting_index.embedding, "embed", side_effect=lambda texts: [[1.0, 2.0] for _ in texts])
        self.embed = embedding.start()
        self.addCleanup(embedding.stop)
        app = FastAPI()
        app.include_router(chapters.router, prefix="/api/v1")
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.url = f"/api/v1/novels/{self.nid}/chapters/{self.cid}"

    def fingerprint(self, *, db=None, novel_id=None, chapter_id=None):
        db, nid, cid = db or self.db, novel_id or self.nid, chapter_id or self.cid
        row = repos.ChapterRepo(db).get(nid, cid)
        return chapter_source_fingerprint(nid, cid, {field: getattr(row, field) for field in ("title", "content", "directive")})

    def apply(self, suggestions, source=None, *, db=None):
        return settle.apply_suggestions(db or self.db, self.nid, suggestions, self.cid,
                                        source_fingerprint=source or self.base)

    def post(self, suggestions, source=None):
        return self.client.post(self.url + "/settle/apply", json={
            "suggestions": suggestions, "source_fingerprint": source or self.base,
        })

    @staticmethod
    def character(name="林舟", **fields):
        return {"type": "character", "data": {"name": name, **fields}}

    def count(self, model):
        return self.db.query(model).filter_by(novel_id=self.nid).count()

    def test_http_requires_original_strict_lowercase_hash(self):
        for payload in ({"suggestions": [self.character()]},
                        *({"suggestions": [self.character()], "source_fingerprint": value}
                          for value in (None, 1, "", "a" * 63, "A" * 64, "g" * 64, "a" * 65))):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post(self.url + "/settle/apply", json=payload).status_code, 422)
        self.assertEqual(self.count(models.Character), 0)
        self.assertEqual(self.count(models.ChapterSettleReceipt), 0)

    def test_source_hash_tracks_exact_author_fields_and_scope_only(self):
        row = repos.ChapterRepo(self.db).get(self.nid, self.cid)
        author = {field: getattr(row, field) for field in ("title", "content", "directive")}
        repos.ChapterRepo(self.db).update(self.nid, self.cid, summary="手改摘要", status="finalized", tension_score=.5, drift_score=.2)
        self.assertEqual(self.fingerprint(), self.base)
        for field in author:
            with self.subTest(field=field):
                changed = {**author, field: author[field] + " "}
                self.assertNotEqual(chapter_source_fingerprint(self.nid, self.cid, changed), self.base)
        self.assertNotEqual(chapter_source_fingerprint(self.other, self.cid, author), self.base)
        self.assertNotEqual(chapter_source_fingerprint(self.nid, "another-chapter", author), self.base)

    def test_settle_rejects_client_snapshot_before_model_or_finalizing_another_saved_draft(self):
        row = repos.ChapterRepo(self.db).get(self.nid, self.cid)
        expected = {field: getattr(row, field) for field in ("title", "content", "directive")}
        with self.Session() as other:
            repos.ChapterRepo(other).update(self.nid, self.cid, content="另一请求先保存的新稿")
        self.db.expire_all()
        with patch.object(settle.llm, "complete", return_value='{"summary":"不应生成的摘要"}') as llm, \
                patch.object(settle, "write_chapter_memory", return_value=MemoryWriteResult(0, 0, 0, "empty")):
            response = self.client.post(self.url + "/settle", json={"expected_author_fields": expected})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "settle_source_changed")
        llm.assert_not_called()
        current = repos.ChapterRepo(self.db).get(self.nid, self.cid)
        self.assertEqual((current.content, current.status, current.summary), ("另一请求先保存的新稿", "draft", ""))

    def test_settle_snapshot_requires_exact_three_string_fields(self):
        for expected in ({"title": "章节", "content": "正文"},
                         {"title": 1, "content": "正文", "directive": "指令"},
                         {"title": "章节", "content": None, "directive": "指令"},
                         {"title": "章节", "content": "正文", "directive": "指令", "summary": "无关字段"}):
            with self.subTest(expected=expected), patch.object(settle.llm, "complete") as llm:
                response = self.client.post(self.url + "/settle", json={"expected_author_fields": expected})
                self.assertEqual(response.status_code, 422)
                llm.assert_not_called()

    def test_settle_snapshot_uses_fresh_database_fields_despite_cached_orm_and_allows_exact_match(self):
        cached = repos.ChapterRepo(self.db).get(self.nid, self.cid)
        old = {field: getattr(cached, field) for field in ("title", "content", "directive")}
        with self.Session() as other:
            repos.ChapterRepo(other).update(self.nid, self.cid, title="另一请求改标题", directive="另一请求改指令")
        self.assertEqual(cached.title, old["title"])
        with patch.object(settle.llm, "complete", return_value='{"summary":"本轮摘要"}') as llm, \
                patch.object(settle, "write_chapter_memory", return_value=MemoryWriteResult(0, 0, 0, "empty")):
            stale = self.client.post(self.url + "/settle", json={"expected_author_fields": old})
            self.assertEqual(stale.status_code, 409)
            llm.assert_not_called()
            expected = {**old, "title": "另一请求改标题", "directive": "另一请求改指令"}
            matching = self.client.post(self.url + "/settle", json={"expected_author_fields": expected})
            self.assertEqual(matching.status_code, 200)
            self.assertEqual(matching.json()["source_fingerprint"], chapter_source_fingerprint(self.nid, self.cid, expected))
            self.assertEqual(matching.json()["summary"], "本轮摘要")
            llm.assert_called_once()

    def test_settle_returns_original_hash_and_repeated_same_draft_replays_every_type(self):
        extracted = {"summary": "灯塔亮起", "characters": [{"name": "林舟"}],
                     "events": [{"title": "入塔", "participants": ["林舟"]}],
                     "foreshadowings": [{"content": "灯号", "related_characters": ["林舟"]}]}
        with patch.object(settle.llm, "complete", return_value=json.dumps(extracted, ensure_ascii=False)) as llm, \
                patch.object(settle, "write_chapter_memory", return_value=MemoryWriteResult(0, 0, 0, "empty")):
            first = self.client.post(self.url + "/settle").json()
            applied = self.post(first["suggestions"], first["source_fingerprint"]).json()
            second = self.client.post(self.url + "/settle").json()
            repeated = self.post(second["suggestions"], second["source_fingerprint"]).json()
        self.assertEqual(first["source_fingerprint"], self.base)
        self.assertEqual(second["source_fingerprint"], self.base)
        self.assertEqual(llm.call_count, 2)
        self.assertEqual(applied["new_count"], 3)
        self.assertEqual(repeated["new_count"], 0)
        self.assertEqual(repeated["replayed_count"], 3)
        self.assertEqual([row["id"] for row in applied["created"]], [row["id"] for row in repeated["created"]])
        self.assertTrue(all(row["replayed"] and not row["deleted"] for row in repeated["created"]))
        self.assertEqual(self.count(models.ChapterSettleReceipt), 3)
        self.assertEqual(self.count(models.MemoryChunk), 3)
        self.assertEqual(self.db.query(models.Foreshadowing).one().planted_chapter_id, self.cid)

    def test_reason_unknown_fields_key_order_and_explicit_defaults_do_not_duplicate(self):
        first = self.apply([{"type": "character", "reason": "提炼理由甲", "data": {
            "name": "林舟", "extras": {"乙": 2, "甲": 1}, "ignored": "不会落库",
        }}])
        second = self.apply([{"data": {"extras": {"甲": 1, "乙": 2}, "role": "", "name": "林舟"},
                              "reason": "提炼理由乙", "type": "character"}])
        self.assertEqual(first["created"][0]["receipt_id"], second["created"][0]["receipt_id"])
        self.assertEqual(second["new_count"], 0)
        self.assertEqual(self.count(models.Character), 1)
        clue = {"type": "foreshadowing", "data": {"content": "灯号"}}
        old = self.apply([clue])
        explicit = self.apply([{"type": "foreshadowing", "data": {
            "content": "灯号", "status": "planted", "planted_chapter_id": self.cid,
        }}])
        self.assertEqual(old["created"][0]["id"], explicit["created"][0]["id"])
        self.assertEqual(explicit["replayed_count"], 1)

    def test_response_loss_whole_batch_retry_replays_success_and_keeps_failure_index(self):
        items = [self.character(), {"type": "timeline_event", "data": {"title": "入塔", "participants": ["未知人物"]}}]
        first = self.apply(items)
        repeated = self.apply(items)
        self.assertEqual(first["new_count"], 1)
        self.assertEqual(repeated["replayed_count"], 1)
        self.assertEqual(repeated["created"][0]["id"], first["created"][0]["id"])
        self.assertEqual(repeated["failed"][0]["index"], 1)
        self.assertIn("未知人物", repeated["failed"][0]["error"])
        repos.CharacterRepo(self.db).create(self.nid, name="未知人物")
        corrected = self.apply(items)
        self.assertEqual(corrected["failed"], [])
        self.assertEqual((corrected["new_count"], corrected["replayed_count"]), (1, 1))
        self.assertEqual(self.count(models.ChapterSettleReceipt), 2)
        self.assertEqual(self.count(models.TimelineEvent), 1)

    def test_duplicate_items_in_one_batch_have_one_entity_and_request_local_indices(self):
        result = self.apply([self.character(), self.character()])
        self.assertEqual([row["index"] for row in result["created"]], [0, 1])
        self.assertEqual(result["created"][0]["id"], result["created"][1]["id"])
        self.assertEqual((result["new_count"], result["replayed_count"]), (1, 1))
        subset = self.apply([self.character()])
        self.assertEqual(subset["created"][0]["index"], 0)
        self.assertTrue(subset["created"][0]["replayed"])

    def test_replay_precedes_changed_deleted_or_ambiguous_reference_resolution(self):
        person = repos.CharacterRepo(self.db).create(self.nid, name="林舟")
        pid = person.id
        item = {"type": "timeline_event", "data": {"title": "入塔", "participants": ["林舟"]}}
        original = self.apply([item])
        setting_index.update_setting(self.db, repos.CharacterRepo, self.nid, pid, name="作者改名")
        setting_index.delete_setting(self.db, repos.CharacterRepo, self.nid, pid)
        repos.CharacterRepo(self.db).create(self.nid, name="林舟")
        repos.CharacterRepo(self.db).create(self.nid, name="林舟")
        repeated = self.apply([item])
        self.assertEqual(repeated["failed"], [])
        self.assertEqual(repeated["created"][0]["id"], original["created"][0]["id"])
        self.assertEqual(self.count(models.TimelineEvent), 1)

    def test_old_unapplied_source_is_http409_and_never_writes_entities(self):
        response = self.client.put(self.url, json={"content": "作者的新稿。"})
        self.assertEqual(response.status_code, 200)
        refused = self.post([self.character()])
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json()["code"], "settle_source_changed")
        self.assertEqual(self.count(models.Character), 0)
        self.assertEqual(self.count(models.ChapterSettleReceipt), 0)

    def test_replayed_old_source_preserves_success_and_refuses_unapplied_remaining_item(self):
        old = self.apply([self.character()])
        self.client.put(self.url, json={"title": "第二稿标题"})
        result = self.post([self.character(), self.character("新人物")])
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertEqual(data["created"][0]["id"], old["created"][0]["id"])
        self.assertEqual((data["new_count"], data["replayed_count"]), (0, 1))
        self.assertEqual(data["failed"][0]["index"], 1)
        self.assertEqual(data["failed"][0]["code"], "settle_source_changed")
        self.assertEqual(self.count(models.Character), 1)

    def test_midbatch_concurrent_author_save_keeps_first_commit_and_refuses_remainder(self):
        real_commit = self.db.commit
        committed = 0

        def commit_and_edit():
            nonlocal committed
            real_commit()
            committed += 1
            if committed == 1:
                with self.Session() as other:
                    repos.ChapterRepo(other).update(self.nid, self.cid, content="另一请求保存的新稿")

        with patch.object(self.db, "commit", side_effect=commit_and_edit):
            result = self.apply([self.character(), self.character("剩余人物")])
        self.assertEqual(result["new_count"], 1)
        self.assertEqual(result["failed"][0]["code"], "settle_source_changed")
        self.assertEqual(result["failed"][0]["index"], 1)
        self.assertEqual(self.count(models.Character), 1)
        self.assertEqual(self.count(models.ChapterSettleReceipt), 1)
        self.assertEqual(repos.ChapterRepo(self.db).get(self.nid, self.cid).content, "另一请求保存的新稿")

    def test_actual_sqlite_failure_rolls_back_entity_index_and_receipt_then_retry_creates_once(self):
        for table in ("chapter_settle_receipt", "memory_chunk"):
            with self.subTest(table=table):
                self.db.execute(text(f"CREATE TRIGGER isolated_refusal BEFORE INSERT ON {table} "
                                     "BEGIN SELECT RAISE(ABORT, 'isolated refusal'); END"))
                self.db.commit()
                name = "故障人物" + table
                failed = self.apply([self.character(name)])
                self.assertEqual(failed["new_count"], 0)
                self.assertEqual(failed["created"], [])
                self.assertIn("未保存", failed["failed"][0]["error"])
                self.assertEqual(self.db.query(models.Character).filter_by(novel_id=self.nid, name=name).count(), 0)
                self.db.execute(text("DROP TRIGGER isolated_refusal")); self.db.commit()
                succeeded = self.apply([self.character(name)])
                self.assertEqual(succeeded["new_count"], 1)
                self.assertEqual(self.apply([self.character(name)])["replayed_count"], 1)
        self.assertEqual(self.count(models.Character), 2)
        self.assertEqual(self.count(models.MemoryChunk), 2)
        self.assertEqual(self.count(models.ChapterSettleReceipt), 2)

    def test_two_real_connections_concurrently_confirm_once(self):
        start = Barrier(2)
        results, errors = [], []

        def confirm():
            try:
                with self.Session() as db:
                    start.wait(timeout=5)
                    results.append(self.apply([self.character()], db=db))
            except Exception as error:
                errors.append(error)

        threads = [Thread(target=confirm) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(not result["failed"] for result in results))
        self.assertEqual(sorted((row["new_count"], row["replayed_count"]) for row in results), [(0, 1), (1, 0)])
        self.assertEqual(results[0]["created"][0]["id"], results[1]["created"][0]["id"])
        self.assertEqual(self.count(models.Character), 1)
        self.assertEqual(self.count(models.MemoryChunk), 1)
        self.assertEqual(self.count(models.ChapterSettleReceipt), 1)

    def test_http_success_survives_real_postcommit_exclusive_lock(self):
        blocker = sqlite3.connect(self.path, isolation_level=None, timeout=.02, check_same_thread=False)
        real_commit = self.db.commit

        def commit_then_lock():
            real_commit()
            blocker.execute("BEGIN EXCLUSIVE")

        try:
            with patch.object(self.db, "commit", side_effect=commit_then_lock):
                response = self.post([self.character()])
            self.assertEqual(response.status_code, 200)
            saved = response.json()
            self.assertEqual(saved["new_count"], 1)
            self.assertEqual(saved["failed"], [])
        finally:
            blocker.rollback(); blocker.close()
        replay = self.post([self.character()]).json()
        self.assertEqual(replay["created"][0]["id"], saved["created"][0]["id"])
        self.assertEqual(replay["replayed_count"], 1)
        self.assertEqual(self.count(models.Character), 1)

    def test_entity_delete_keeps_receipt_tombstone_and_does_not_rebind_same_name(self):
        old = self.apply([self.character()])["created"][0]
        setting_index.delete_setting(self.db, repos.CharacterRepo, self.nid, old["id"])
        new_id = repos.CharacterRepo(self.db).create(self.nid, name="林舟").id
        replay = self.apply([self.character()])
        self.assertEqual(replay["created"][0]["id"], old["id"])
        self.assertNotEqual(old["id"], new_id)
        self.assertTrue(replay["created"][0]["deleted"])
        self.assertEqual(replay["new_count"], 0)
        self.assertEqual(self.count(models.Character), 1)
        self.assertEqual(self.count(models.ChapterSettleReceipt), 1)

    def test_actual_author_edit_and_restore_keep_receipts_and_original_source_identity(self):
        old = self.apply([self.character()])
        self.client.put(self.url, json={"content": "作者另写一稿"})
        self.assertEqual(self.count(models.ChapterSettleReceipt), 1)
        revision = self.client.get(self.url + "/revisions").json()[0]
        restored = self.client.post(self.url + f"/revisions/{revision['id']}/restore")
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json()["status"], "draft")
        self.assertEqual(self.fingerprint(), self.base)
        replay = self.apply([self.character()])
        self.assertEqual(replay["created"][0]["id"], old["created"][0]["id"])
        self.assertEqual(self.count(models.Character), 1)

    def test_changed_same_name_suggestion_requires_review_while_manual_crud_remains_legal(self):
        result = self.apply([self.character(role="主角"), self.character(role="配角")])
        self.assertEqual(result["new_count"], 1)
        self.assertEqual(result["failed"][0]["code"], "settle_name_exists")
        self.assertEqual(result["failed"][0]["existing_ids"], [result["created"][0]["id"]])
        self.assertEqual(self.db.query(models.Character).one().role, "主角")
        repos.CharacterRepo(self.db).create(self.nid, name="林舟", role="作者另设")
        self.assertEqual(self.count(models.Character), 2)
        event = self.apply([{"type": "timeline_event", "data": {"title": "入塔", "participants": ["林舟"]}}])
        self.assertIn("同名", event["failed"][0]["error"])
        self.assertEqual(self.count(models.TimelineEvent), 0)

    def test_existing_main_identifiers_fail_for_each_type_without_merging_or_creating(self):
        original = [self.character(), {"type": "timeline_event", "data": {"title": "入塔", "description": "旧事件"}},
                    {"type": "foreshadowing", "data": {"content": "灯号", "status": "planted"}}]
        saved = self.apply(original)
        changed = [self.character(personality="新性格"),
                   {"type": "timeline_event", "data": {"title": "入塔", "description": "新事件"}},
                   {"type": "foreshadowing", "data": {"content": "灯号", "status": "consumed"}}]
        result = self.apply(changed)
        self.assertEqual(result["created"], [])
        self.assertEqual([row["code"] for row in result["failed"]], ["settle_name_exists"] * 3)
        self.assertEqual([row["existing_ids"] for row in result["failed"]], [[row["id"]] for row in saved["created"]])
        self.assertEqual(self.count(models.ChapterSettleReceipt), 3)
        self.assertEqual(self.db.query(models.TimelineEvent).one().description, "旧事件")
        self.assertEqual(self.db.query(models.Foreshadowing).one().status, "planted")

    def test_new_draft_same_name_requires_review_but_replay_still_precedes_conflicts(self):
        old = self.apply([self.character()])
        mixed = self.apply([self.character(), self.character(role="模型改了描述"), self.character("新人物")])
        self.assertEqual((mixed["new_count"], mixed["replayed_count"]), (1, 1))
        self.assertEqual(mixed["created"][0]["id"], old["created"][0]["id"])
        self.assertEqual(mixed["failed"][0]["code"], "settle_name_exists")
        self.client.put(self.url, json={"content": "作者第三稿"})
        new_hash = self.fingerprint()
        new_draft = self.apply([self.character()], new_hash)
        self.assertEqual(new_draft["created"], [])
        self.assertEqual(new_draft["failed"][0]["existing_ids"], [old["created"][0]["id"]])
        self.assertEqual(self.count(models.Character), 2)
        stale = self.post([self.character(role="改描述")])
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["code"], "settle_source_changed")

    def test_name_collision_returns_all_own_ids_and_does_not_include_foreign_names(self):
        ids = [repos.CharacterRepo(self.db).create(self.nid, name="手工同名").id for _ in range(2)]
        foreign = repos.CharacterRepo(self.db).create(self.other, name="手工同名").id
        rejected = self.apply([self.character("手工同名")])
        self.assertEqual(rejected["failed"][0]["existing_ids"], sorted(ids))
        self.assertNotIn(foreign, rejected["failed"][0]["existing_ids"])
        repos.CharacterRepo(self.db).create(self.other, name="外国已有")
        self.assertEqual(self.apply([self.character("外国已有")])["new_count"], 1)

    def test_hash_cannot_be_reused_for_another_chapter_or_foreign_work(self):
        other_chapter = repos.ChapterRepo(self.db).create(self.nid, title="第一章", content="林舟走进灯塔。", directive="灯号必须出现。").id
        response = self.client.post(f"/api/v1/novels/{self.nid}/chapters/{other_chapter}/settle/apply", json={
            "source_fingerprint": self.base, "suggestions": [self.character()],
        })
        self.assertEqual(response.status_code, 409)
        response = self.client.post(f"/api/v1/novels/{self.other}/chapters/{self.cid}/settle/apply", json={
            "source_fingerprint": self.base, "suggestions": [self.character()],
        })
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.db.query(models.Character).count(), 0)

    def test_chapter_and_novel_deletion_cleanup_only_their_receipt_scope(self):
        self.apply([self.character()])
        foreign_cid = repos.ChapterRepo(self.db).create(self.other, title="外国章节").id
        other_hash = self.fingerprint(novel_id=self.other, chapter_id=foreign_cid)
        foreign = settle.apply_suggestions(self.db, self.other, [self.character("外国人物")], foreign_cid, source_fingerprint=other_hash)
        another_cid = repos.ChapterRepo(self.db).create(self.nid, title="另一章节").id
        another_hash = self.fingerprint(chapter_id=another_cid)
        settle.apply_suggestions(self.db, self.nid, [self.character("另一人物")], another_cid, source_fingerprint=another_hash)
        self.assertEqual(self.count(models.ChapterSettleReceipt), 2)
        self.assertEqual(self.client.delete(self.url).status_code, 204)
        self.assertEqual(self.count(models.ChapterSettleReceipt), 1)
        self.assertEqual(self.count(models.Character), 2)
        repos.NovelRepo(self.db).delete(self.nid)
        remaining = self.db.query(models.ChapterSettleReceipt).all()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].entity_id, foreign["created"][0]["id"])
        self.assertEqual(self.db.query(models.Character).filter_by(novel_id=self.other).count(), 1)

    def test_rejecting_caller_flushed_or_core_writes_preserves_their_transaction(self):
        for mode in ("flushed_orm", "core"):
            with self.subTest(mode=mode):
                if mode == "flushed_orm":
                    pending = models.Character(novel_id=self.nid, name="调用方人物")
                    self.db.add(pending); self.db.flush()
                    pending_id = pending.id
                else:
                    self.db.execute(text("UPDATE novel SET summary='调用方DML' WHERE id=:nid"), {"nid": self.nid})
                with self.assertRaises(RuntimeError):
                    self.apply([self.character()])
                if mode == "flushed_orm":
                    self.assertIsNotNone(self.db.get(models.Character, pending_id))
                else:
                    self.assertEqual(self.db.execute(text("SELECT summary FROM novel WHERE id=:nid"), {"nid": self.nid}).scalar_one(), "调用方DML")
                self.db.rollback()
        self.assertEqual(self.count(models.ChapterSettleReceipt), 0)

    def test_embedding_failure_commits_fact_and_receipt_without_stale_index(self):
        with patch.object(setting_index.embedding, "embed", side_effect=RuntimeError("离线")):
            saved = self.apply([self.character()])
        self.assertEqual(saved["new_count"], 1)
        self.assertEqual(self.count(models.MemoryChunk), 0)
        self.assertEqual(self.count(models.ChapterSettleReceipt), 1)
        self.embed.reset_mock()
        self.assertEqual(self.apply([self.character()])["replayed_count"], 1)
        self.embed.assert_not_called()

    def test_empty_batch_has_zero_counts_and_malformed_suggestions_do_not_leave_receipts(self):
        self.assertEqual(self.apply([]), {"created": [], "failed": [], "new_count": 0, "replayed_count": 0})
        result = self.apply([{"type": "character", "data": {}}, {"type": "unknown", "data": {}},
                             {"type": "timeline_event", "data": []}, self.character(extras=[])])
        self.assertEqual([row["index"] for row in result["failed"]], [0, 1, 2, 3])
        self.assertEqual(self.count(models.Character), 0)
        self.assertEqual(self.count(models.ChapterSettleReceipt), 0)

    def test_old_database_migration_twice_preserves_author_data_and_existing_receipt(self):
        with TemporaryDirectory(prefix="lingmo-legacy-receipts-") as directory:
            path = Path(directory) / "old.sqlite"
            engine = create_engine(f"sqlite:///{path.as_posix()}", poolclass=NullPool)
            self.addCleanup(engine.dispose)
            models.Novel.__table__.create(engine)
            models.Character.__table__.create(engine)
            with engine.begin() as conn:
                conn.execute(text("CREATE TABLE chapter (id VARCHAR(36) PRIMARY KEY, novel_id VARCHAR(36), "
                                  "outline_node_id VARCHAR(36), seq INTEGER, title VARCHAR(200), content TEXT, "
                                  "status VARCHAR(20), summary TEXT, tension_score FLOAT, directive TEXT, "
                                  "created_at VARCHAR(40), updated_at VARCHAR(40))"))
                conn.execute(text("INSERT INTO chapter VALUES ('chapter-old','novel-old','',1,'旧标题','作者旧稿',"
                                  "'finalized','旧摘要',.5,'旧指令','旧创建时间','旧更新时间')"))
            Session = sessionmaker(bind=engine, autoflush=False)
            with Session() as db:
                db.add(models.Novel(id="novel-old", title="旧作品"))
                db.add_all([models.Character(novel_id="novel-old", name="合法同名", role="甲"),
                            models.Character(novel_id="novel-old", name="合法同名", role="乙")]); db.commit()
            with engine.connect() as conn:
                original = [tuple(row) for row in conn.execute(text("SELECT * FROM chapter"))]
                old_characters = [tuple(row) for row in conn.execute(text("SELECT * FROM character ORDER BY id"))]
            with patch.object(database, "engine", engine): database.init_db()
            with Session() as db:
                source = self.fingerprint(db=db, novel_id="novel-old", chapter_id="chapter-old")
                saved = settle.apply_suggestions(db, "novel-old", [self.character("新确认人物")], "chapter-old", source_fingerprint=source)
                receipt_id = saved["created"][0]["receipt_id"]
            with patch.object(database, "engine", engine): database.init_db()
            with engine.connect() as conn:
                rows = [tuple(row) for row in conn.execute(text(
                    "SELECT id,novel_id,outline_node_id,seq,title,content,status,summary,tension_score,directive,created_at,updated_at FROM chapter"
                ))]
                self.assertEqual(rows, original)
                remaining = [tuple(row) for row in conn.execute(text("SELECT * FROM character WHERE name='合法同名' ORDER BY id"))]
                self.assertEqual(remaining, old_characters)
                self.assertEqual(conn.execute(text("SELECT id FROM chapter_settle_receipt")).scalar_one(), receipt_id)
            with Session() as db:
                replay = settle.apply_suggestions(db, "novel-old", [self.character("新确认人物")], "chapter-old", source_fingerprint=source)
                self.assertEqual(replay["replayed_count"], 1)


if __name__ == "__main__":
    unittest.main()
