"""生成预览的实际来源和精确选区；HTTP/SSE/物理SQLite回归。"""
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Thread
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from app.application import revisions, writing
from app.infrastructure import models, repository as repos
from app.infrastructure.db import get_db
from app.interfaces.api.v1 import chapters
from app.interfaces.schemas import ChapterUpdate


def frames(body):
    return [json.loads(line[5:].strip()) for line in body.splitlines() if line.startswith("data:")]


class RewritePreviewSourceTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "rewrite-source.db"
        self.engine = create_engine(f"sqlite:///{self.path.as_posix()}",
                                   connect_args={"check_same_thread": False, "timeout": .1})
        self.addCleanup(self.engine.dispose)
        models.Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        with self.sessions() as db:
            self.nid = repos.NovelRepo(db).create(title="精确段落来源作品").id
            self.other = repos.NovelRepo(db).create(title="其它作品").id
            self.original = "😀\x00章首。 同样段落。 中间新线索。 同样段落。 章尾👨‍👩‍👧‍👦。"
            self.cid = repos.ChapterRepo(db).create(self.nid, title="原标题", content=self.original,
                directive="原章前要求", status="finalized").id
            repos.ChapterRepo(db).update(self.nid, self.cid, summary="已存摘要", tension_score=7, drift_score=.4)
            self.other_cid = repos.ChapterRepo(db).create(self.other, title="其它标题", content="其它正文").id
            db.add(models.MemoryChunk(novel_id=self.nid, source_chapter_id=self.cid, text="旧正文记忆",
                                      vector=[1.0, 0.0]))
            db.add(models.MemoryChunk(novel_id=self.other, source_chapter_id=self.other_cid, text="其它记忆",
                                      vector=[1.0, 0.0]))
            db.commit()
        self.source = {"title": "原标题", "content": self.original, "directive": "原章前要求"}
        self.passage = " 同样段落。 "
        self.start = self.original.rindex(self.passage)
        self.end = self.start + len(self.passage)
        self.generated = " 精确改写后。 "
        self.app = FastAPI()
        self.app.include_router(chapters.router, prefix="/api/v1")

        def db():
            with self.sessions() as session:
                yield session
        self.app.dependency_overrides[get_db] = db
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        for mocked in (patch.object(chapters, "SessionLocal", self.sessions),
                       patch.object(writing, "build_context", return_value="受控本作品事实")):
            mocked.start()
            self.addCleanup(mocked.stop)
        self.base = f"/api/v1/novels/{self.nid}/chapters/{self.cid}"

    def _request(self, **changes):
        return {"passage": self.passage, "preview": True, "expected_author_fields": dict(self.source),
                "selection_start": self.start, "selection_end": self.end, **changes}

    def _preview(self, payload=None):
        with patch.object(writing.llm, "stream_complete", return_value=iter([self.generated])) as provider:
            response = self.client.post(self.base + "/rewrite", json=payload or self._request())
        return response, provider.call_count

    def _done(self):
        response, count = self._preview()
        self.assertEqual((response.status_code, count), (200, 1), response.text)
        return next(row for row in frames(response.text) if row["type"] == "done")

    def _apply(self, done, **changes):
        return self.client.put(self.base, json={"content": done["content"], "revision_source": "rewrite_apply",
            "expected_author_fields": done["expected_author_fields"], **changes})

    def _rows(self):
        with self.sessions() as db:
            return {table.name: [dict(row) for row in db.execute(select(table)).mappings()]
                    for table in models.Base.metadata.sorted_tables}

    def test_second_identical_passage_unicode_nul_emoji_and_whitespace_exact(self):
        before = self._rows()
        done = self._done()
        self.assertEqual(done["expected_author_fields"], self.source)
        self.assertEqual((done["selection_start"], done["selection_end"]), (self.start, self.end))
        self.assertEqual(done["chapter"]["content"], self.original)
        expected = self.original[:self.start] + self.generated + self.original[self.end:]
        self.assertEqual(done["content"], expected)
        self.assertEqual(self._rows(), before)
        response = self._apply(done)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["content"], expected)
        self.assertTrue(response.json()["content"].startswith(self.original[:self.start]))
        after = self._rows()
        revision = next(row for row in after["chapter_revision"] if row["chapter_id"] == self.cid)
        self.assertEqual((revision["source"], revision["content"], revision["status"], revision["summary"],
                          revision["tension_score"], revision["drift_score"]),
                         ("rewrite_apply", self.original, "finalized", "已存摘要", 7, .4))
        self.assertEqual((response.json()["status"], response.json()["summary"], response.json()["tension_score"],
                          response.json()["drift_score"]), ("draft", "", None, None))
        self.assertFalse(any(row["source_chapter_id"] == self.cid for row in after["memory_chunk"]))
        self.assertEqual([row for row in after["chapter"] if row["novel_id"] == self.other],
                         [row for row in before["chapter"] if row["novel_id"] == self.other])
        self.assertEqual([row for row in after["memory_chunk"] if row["novel_id"] == self.other],
                         [row for row in before["memory_chunk"] if row["novel_id"] == self.other])

    def test_pre_model_conflict_each_author_field_zero_calls_zero_write(self):
        for field in self.source:
            with self.subTest(field=field):
                with self.sessions() as db:
                    repos.ChapterRepo(db).update(self.nid, self.cid, **{field: self.source[field] + "另请求新值"})
                before = self._rows()
                response, calls = self._preview()
                self.assertEqual(calls, 0)
                error = frames(response.text)[-1]
                self.assertEqual((error["code"], error["status"]), ("chapter_write_conflict", 409))
                self.assertEqual(self._rows(), before)
                with self.sessions() as db:
                    repos.ChapterRepo(db).update(self.nid, self.cid, **{field: self.source[field]})

    def test_stale_cached_orm_cannot_replace_true_database_source(self):
        with self.sessions() as db:
            cached = repos.ChapterRepo(db).get(self.nid, self.cid)
            with self.sessions() as writer:
                repos.ChapterRepo(writer).update(self.nid, self.cid, title="数据库真实新标题")
            expected = {**self.source, "title": "数据库真实新标题"}
            with patch.object(writing.llm, "stream_complete", return_value=iter([self.generated])):
                emitted = list(writing.rewrite_passage(db, self.nid, self.cid, self.passage, preview=True,
                    expected_author_fields=expected, selection_start=self.start, selection_end=self.end))
            done = frames("".join(emitted))[-1]
            self.assertEqual(done["type"], "done")
            self.assertEqual(done["expected_author_fields"], expected)
            self.assertEqual(done["chapter"]["title"], "数据库真实新标题")
            self.assertEqual(cached.title, self.source["title"])

    def test_range_schema_rejects_bool_float_missing_pair_negative_reverse_and_outside(self):
        invalid = [{"selection_start": True}, {"selection_end": False}, {"selection_start": 1.0},
                   {"selection_end": "20"}, {"selection_start": None}, {"selection_end": None},
                   {"selection_start": -1}, {"selection_end": 0}, {"selection_start": self.end},
                   {"selection_end": len(self.original) + 1}, {"expected_author_fields": {"content": self.original}},
                   {"expected_author_fields": {**self.source, "title": 1}}]
        before = self._rows()
        with patch.object(writing.llm, "stream_complete") as provider:
            for changes in invalid:
                with self.subTest(changes=changes):
                    response = self.client.post(self.base + "/rewrite", json=self._request(**changes))
                    self.assertEqual(response.status_code, 422, response.text)
            provider.assert_not_called()
        self.assertEqual(self._rows(), before)

    def test_range_mismatch_and_outside_without_expected_source_zero_model(self):
        for changes in ({"passage": self.passage.strip()}, {"selection_start": self.start - 1},
                        {"expected_author_fields": None, "selection_end": len(self.original) + 1}):
            with self.subTest(changes=changes):
                response, count = self._preview(self._request(**changes))
                self.assertEqual(count, 0)
                self.assertEqual(frames(response.text)[-1]["code"], "chapter_rewrite_selection_invalid")

    def test_legacy_preview_ambiguous_rejects_but_unique_returns_canonical_range(self):
        ambiguous, count = self._preview({"passage": self.passage, "preview": True})
        self.assertEqual(count, 0)
        self.assertEqual(frames(ambiguous.text)[-1]["code"], "chapter_rewrite_selection_invalid")
        unique = "中间新线索"
        response, count = self._preview({"passage": unique, "preview": True})
        self.assertEqual(count, 1)
        done = frames(response.text)[-1]
        self.assertEqual((done["selection_start"], done["selection_end"]),
                         (self.original.index(unique), self.original.index(unique) + len(unique)))
        self.assertEqual(done["expected_author_fields"], self.source)

    def test_legacy_direct_rewrite_without_range_keeps_first_match_contract(self):
        response, count = self._preview({"passage": self.passage, "preview": False})
        self.assertEqual(count, 1)
        done = frames(response.text)[-1]
        self.assertEqual(done["chapter"]["content"], self.original.replace(self.passage, self.generated, 1))

    def test_current_title_or_directive_change_after_preview_rejects_apply_atomically(self):
        for field in ("title", "directive", "content"):
            with self.subTest(field=field):
                done = self._done()
                changed = self.client.put(self.base, json={field: self.source[field] + "作者新保存"})
                self.assertEqual(changed.status_code, 200)
                before = self._rows()
                response = self._apply(done)
                self.assertEqual((response.status_code, response.json()["code"]), (409, "chapter_write_conflict"))
                self.assertEqual(self._rows(), before)
                restored = self.client.put(self.base, json={field: self.source[field]})
                self.assertEqual(restored.status_code, 200)

    def test_real_model_wait_does_not_lock_other_writer_and_late_apply_preserves_new_body(self):
        started, release = Event(), Event()
        responses, errors = [], []

        def provider(*_args, **_kwargs):
            started.set()
            if not release.wait(5):
                raise RuntimeError("测试模型门闩超时")
            yield self.generated

        def request():
            try:
                responses.append(self.client.post(self.base + "/rewrite", json=self._request()))
            except Exception as error:
                errors.append(error)
        with patch.object(writing.llm, "stream_complete", side_effect=provider):
            worker = Thread(target=request)
            worker.start()
            try:
                self.assertTrue(started.wait(3))
                changed_body = "作者新保存的章首。" + self.original + "作者新保存的章尾。"
                changed = self.client.put(self.base, json={"content": changed_body})
                self.assertEqual(changed.status_code, 200, changed.text)
            finally:
                release.set()
                worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
        done = frames(responses[0].text)[-1]
        self.assertEqual(done["expected_author_fields"], self.source)
        before = self._rows()
        applied = self._apply(done)
        self.assertEqual((applied.status_code, applied.json()["code"]), (409, "chapter_write_conflict"))
        self.assertEqual(self.client.get(self.base).json()["content"], changed_body)
        self.assertEqual(self._rows(), before)

    def test_apply_missing_or_incomplete_source_rejected_without_write(self):
        before = self._rows()
        for expected in (None, {}, {"content": self.original}, {**self.source, "directive": None},
                         {**self.source, "extra": "unknown"}):
            payload = {"content": "不能覆盖", "revision_source": "rewrite_apply"}
            if expected is not None:
                payload["expected_author_fields"] = expected
            self.assertEqual(self.client.put(self.base, json=payload).status_code, 422)
        self.assertEqual(self._rows(), before)

    def test_non_author_summary_status_score_changes_do_not_falsely_block_apply(self):
        done = self._done()
        with self.sessions() as db:
            repos.ChapterRepo(db).update(self.nid, self.cid, summary="作者新摘要", status="draft",
                                        tension_score=8, drift_score=.5)
        response = self._apply(done)
        self.assertEqual(response.status_code, 200, response.text)
        version = self._rows()["chapter_revision"][0]
        self.assertEqual((version["summary"], version["status"], version["tension_score"], version["drift_score"]),
                         ("作者新摘要", "draft", 8, .5))

    def test_manual_save_without_expectation_remains_explicit_overwrite(self):
        self.client.put(self.base, json={"content": "其它请求当前稿"})
        response = self.client.put(self.base, json={"content": "作者明确点击手工覆盖稿", "revision_source": "manual_save"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], "作者明确点击手工覆盖稿")

    def test_true_two_connection_concurrent_apply_only_one_updates_and_snapshots(self):
        done = self._done()
        barrier, results, errors = Barrier(3), [], []

        def apply(suffix):
            try:
                barrier.wait(timeout=3)
                results.append(self._apply(done, content=done["content"] + suffix))
            except Exception as error:
                errors.append(error)
        workers = [Thread(target=apply, args=(suffix,)) for suffix in ("请求甲", "请求乙")]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=3)
        for worker in workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sorted(response.status_code for response in results), [200, 409])
        self.assertEqual(len(self._rows()["chapter_revision"]), 1)
        self.assertEqual(self.client.get(self.base).json()["content"], next(row.json()["content"] for row in results if row.status_code == 200))

    def test_actual_snapshot_trigger_failure_rolls_back_content_version_and_memory(self):
        done = self._done()
        before = self._rows()
        with self.engine.begin() as db:
            db.exec_driver_sql("""CREATE TRIGGER reject_rewrite_version BEFORE INSERT ON chapter_revision
                BEGIN SELECT RAISE(ABORT, 'controlled snapshot rejection'); END""")
        self.assertEqual(self._apply(done).status_code, 500)
        self.assertEqual(self._rows(), before)

    def test_actual_memory_delete_trigger_failure_rolls_back_content_and_snapshot(self):
        done = self._done()
        before = self._rows()
        with self.engine.begin() as db:
            db.exec_driver_sql("""CREATE TRIGGER reject_rewrite_memory BEFORE DELETE ON memory_chunk
                BEGIN SELECT RAISE(ABORT, 'controlled memory rejection'); END""")
        self.assertEqual(self._apply(done).status_code, 500)
        self.assertEqual(self._rows(), before)

    def test_real_exclusive_lock_after_commit_cannot_deny_saved_application(self):
        done = self._done()
        original_commit = self.sessions.class_.commit
        blockers, post_sql = [], []
        committed = Event()

        def lock(db):
            original_commit(db)
            blocker = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
            blocker.execute("BEGIN EXCLUSIVE")
            blockers.append(blocker)
            committed.set()

        def sql(_conn, _cursor, statement, *_args):
            if committed.is_set():
                post_sql.append(statement)
        event.listen(self.engine, "before_cursor_execute", sql)
        try:
            with patch.object(self.sessions.class_, "commit", new=lock):
                response = self._apply(done)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["content"], done["content"])
            self.assertEqual(post_sql, [])
        finally:
            for blocker in blockers:
                blocker.rollback()
                blocker.close()
            event.remove(self.engine, "before_cursor_execute", sql)
        self.assertEqual(self.client.get(self.base).json()["content"], done["content"])

    def test_apply_guards_caller_pending_flushed_core_and_delete_before_any_flush(self):
        payload = ChapterUpdate(content="不可触碰调用方事务", revision_source="rewrite_apply", expected_author_fields=self.source)
        for mode in ("new", "dirty", "flushed", "core", "deleted"):
            with self.subTest(mode=mode), self.sessions(autoflush=True) as db:
                if mode in ("new", "flushed"):
                    db.add(models.Character(novel_id=self.nid, name="调用方人物"))
                    if mode == "flushed":
                        db.flush()
                elif mode == "core":
                    db.execute(text("UPDATE novel SET title='调用方未提交书名' WHERE id=:id"), {"id": self.nid})
                else:
                    current = repos.ChapterRepo(db).get(self.nid, self.cid)
                    if mode == "dirty":
                        current.summary = "调用方未提交摘要"
                    else:
                        db.delete(current)
                with patch.object(db, "flush", wraps=db.flush) as flush, \
                        patch.object(db, "commit", wraps=db.commit) as commit, \
                        patch.object(db, "rollback", wraps=db.rollback) as rollback:
                    with self.assertRaises(RuntimeError):
                        chapters.update_chapter(self.nid, self.cid, payload, db)
                    flush.assert_not_called()
                    commit.assert_not_called()
                    rollback.assert_not_called()
                self.assertTrue(db.in_transaction())
                db.rollback()

    def test_preview_preserves_unflushed_flushed_and_core_caller_data_on_success_or_conflict(self):
        for mode in ("new", "dirty", "flushed", "core"):
            with self.subTest(mode=mode), self.sessions(autoflush=True) as db:
                if mode in ("new", "flushed"):
                    db.add(models.Character(novel_id=self.nid, name="调用方人物"))
                    if mode == "flushed":
                        db.flush()
                elif mode == "dirty":
                    current = repos.ChapterRepo(db).get(self.nid, self.cid)
                    current.title = "尚未flush的作者题目"
                else:
                    db.execute(text("UPDATE chapter SET title='已执行未提交题目' WHERE id=:id"), {"id": self.cid})
                with patch.object(writing.llm, "stream_complete", return_value=iter([self.generated])) as provider, \
                        patch.object(db, "flush", wraps=db.flush) as flush, \
                        patch.object(db, "commit", wraps=db.commit) as commit, \
                        patch.object(db, "rollback", wraps=db.rollback) as rollback:
                    values = frames("".join(writing.rewrite_passage(db, self.nid, self.cid, self.passage, preview=True,
                        expected_author_fields=self.source, selection_start=self.start, selection_end=self.end)))
                    self.assertEqual(values[-1]["type"], "error" if mode == "core" else "done")
                    self.assertEqual(provider.call_count, 0 if mode == "core" else 1)
                    flush.assert_not_called()
                    commit.assert_not_called()
                    rollback.assert_not_called()
                self.assertTrue(db.in_transaction())
                db.commit()  # 调用方依然可自行提交，证明其数据没有被清理掉。
                with self.sessions() as observer:
                    if mode in ("new", "flushed"):
                        self.assertGreater(observer.query(models.Character).filter_by(name="调用方人物").count(), 0)
                if mode == "dirty":
                    self.source = {**self.source, "title": "尚未flush的作者题目"}
                elif mode == "core":
                    self.source = {**self.source, "title": "已执行未提交题目"}

    def test_foreign_scope_preview_and_guarded_application_do_not_write(self):
        before = self._rows()
        foreign = f"/api/v1/novels/{self.other}/chapters/{self.cid}"
        with patch.object(writing.llm, "stream_complete") as provider:
            response = self.client.post(foreign + "/rewrite", json=self._request())
            self.assertEqual(frames(response.text)[-1]["type"], "error")
            provider.assert_not_called()
        response = self.client.put(foreign, json={"content": "不该写入", "revision_source": "rewrite_apply", "expected_author_fields": self.source})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._rows(), before)


if __name__ == "__main__":
    unittest.main()
