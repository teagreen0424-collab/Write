"""真实作者修改：派生状态、版本串行写入及流式来源冲突。"""
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Thread
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.application import context, revisions, writing
from app.infrastructure import models, repository as repos
from app.infrastructure.db import get_db
from app.main import app


def events(stream):
    return [json.loads(line[6:]) for line in stream if line.startswith("data: ")]


class AuthorDerivedDataTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        models.Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.Session()
        self.nid = repos.NovelRepo(self.db).create(title="作者修改作品").id
        self.previous_override = app.dependency_overrides.get(get_db)

        def isolated_db():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = isolated_db
        self.client = TestClient(app)

    def tearDown(self):
        if self.previous_override is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = self.previous_override
        self.client.close()
        self.db.close()
        self.engine.dispose()

    def chapter(self, **extra):
        fields = {"title": "原标题", "content": "林越死于北城。", "directive": "保持悬念",
                  "status": "finalized", "summary": "林越已死，后续不会出现。", "tension_score": 8.8, "drift_score": 0.2}
        fields.update(extra)
        chapter = repos.ChapterRepo(self.db).create(self.nid, **fields)
        repos.MemoryChunkRepo(self.db).create(
            self.nid, kind="chapter_slice", source_chapter_id=chapter.id, text=chapter.content, vector=[1.0]
        )
        return chapter

    def rows(self, chapter_id):
        self.db.expire_all()
        return repos.ChapterRevisionRepo(self.db).list_by_chapter(self.nid, chapter_id)

    def test_put_actual_author_changes_invalidates_derived_and_keeps_old_snapshot(self):
        for field, value in (("content", "林越平安离开北城。"), ("title", "新标题"), ("directive", "取消悬念")):
            with self.subTest(field=field):
                chapter = self.chapter()
                chapter_id = chapter.id
                old = {key: getattr(chapter, key) for key in revisions.AUTHOR_FIELDS}
                response = self.client.put(f"/api/v1/novels/{self.nid}/chapters/{chapter_id}", json={field: value})
                self.assertEqual(response.status_code, 200, response.text)
                saved = response.json()
                self.assertEqual(saved[field], value)
                self.assertEqual((saved["status"], saved["summary"], saved["tension_score"], saved["drift_score"]),
                                 ("draft", "", None, None))
                snapshot = self.rows(chapter_id)[0]
                self.assertEqual({key: getattr(snapshot, key) for key in old}, old)
                self.assertEqual((snapshot.status, snapshot.summary, snapshot.tension_score, snapshot.drift_score),
                                 ("finalized", "林越已死，后续不会出现。", 8.8, 0.2))
                self.assertEqual(self.db.query(models.MemoryChunk).filter_by(source_chapter_id=chapter_id).count(), 0)

    def test_author_update_cannot_keep_finalized_or_attached_old_summary(self):
        chapter_id = self.chapter().id
        response = self.client.put(f"/api/v1/novels/{self.nid}/chapters/{chapter_id}", json={
            "content": "新正文", "status": "finalized", "summary": "请求附带旧摘要",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["status"], response.json()["summary"]), ("draft", ""))

    def test_identical_author_save_keeps_derived_and_memory_and_creates_no_version(self):
        chapter = self.chapter()
        chapter_id = chapter.id
        response = self.client.put(f"/api/v1/novels/{self.nid}/chapters/{chapter_id}",
                                   json={key: getattr(chapter, key) for key in revisions.AUTHOR_FIELDS})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["status"], response.json()["summary"], response.json()["drift_score"]),
                         ("finalized", "林越已死，后续不会出现。", 0.2))
        self.assertEqual(self.rows(chapter_id), [])
        self.assertEqual(self.db.query(models.MemoryChunk).filter_by(source_chapter_id=chapter_id).count(), 1)

    def test_summary_status_only_edits_do_not_create_revision_clear_scores_or_settle(self):
        chapter_id = self.chapter().id
        with patch("app.application.settle.llm.complete") as model:
            for changes in ({"summary": "作者校正摘要"}, {"status": "draft"}, {"status": "finalized"}):
                response = self.client.put(f"/api/v1/novels/{self.nid}/chapters/{chapter_id}", json=changes)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["tension_score"], 8.8)
                self.assertEqual(response.json()["drift_score"], 0.2)
        model.assert_not_called()
        self.assertEqual(self.rows(chapter_id), [])
        self.assertEqual(self.db.query(models.MemoryChunk).filter_by(source_chapter_id=chapter_id).count(), 1)

    def test_modified_finalized_chapter_no_longer_supplies_old_context_or_default_export(self):
        chapter_id = self.chapter(seq=1).id
        other = self.chapter(seq=2, title="保留定稿章", content="其他已定稿正文", summary="其他章摘要")
        other_id = other.id
        edited = "林越平安离开北城，并参加下一场决战。"
        response = self.client.put(f"/api/v1/novels/{self.nid}/chapters/{chapter_id}", json={"content": edited})
        self.assertEqual(response.status_code, 200, response.text)
        self.db.expire_all()
        prompt = context.build_context(self.db, self.nid, directive="继续第三章")
        history = context.recent_history(repos.ChapterRepo(self.db).list(self.nid), repos.ChapterRepo(self.db).get(self.nid, other_id))
        self.assertNotIn("林越已死", prompt)
        self.assertNotIn("林越已死", history)
        self.assertIn(edited, history)
        preflight = self.client.post(f"/api/v1/novels/{self.nid}/export/preflight", json={
            "scope": "chapters", "chapter_ids": [chapter_id], "include_drafts": False, "format": "txt",
        })
        self.assertEqual(preflight.status_code, 200, preflight.text)
        self.assertFalse(preflight.json()["ready"])
        self.assertEqual(preflight.json()["chapter_count"], 0)
        self.assertEqual(preflight.json()["draft_chapters"][0]["id"], chapter_id)
        exported = self.client.get(f"/api/v1/novels/{self.nid}/export/txt")
        self.assertEqual(exported.status_code, 200)
        self.assertNotIn(edited, exported.text)
        self.assertIn("其他已定稿正文", exported.text)
        summary = self.client.get(f"/api/v1/novels/{self.nid}/book/summary").json()
        self.assertEqual((summary["draft_count"], summary["finalized_count"]), (1, 1))
        self.assertEqual(self.db.query(models.MemoryChunk).filter_by(source_chapter_id=other_id).count(), 1)

    def test_memory_cleanup_failure_rolls_back_author_snapshot_and_derived_together(self):
        chapter_id = self.chapter().id
        with patch.object(revisions, "delete_chapter_memory", side_effect=RuntimeError("清理失败")):
            with self.assertRaises(RuntimeError):
                revisions.update_chapter_with_revision(self.db, self.nid, chapter_id, {"content": "不能落库"}, revisions.SOURCE_MANUAL_SAVE)
        self.db.expire_all()
        current = repos.ChapterRepo(self.db).get(self.nid, chapter_id)
        self.assertEqual((current.content, current.status, current.summary), ("林越死于北城。", "finalized", "林越已死，后续不会出现。"))
        self.assertEqual(self.rows(chapter_id), [])
        self.assertEqual(self.db.query(models.MemoryChunk).filter_by(source_chapter_id=chapter_id).count(), 1)

    def test_author_change_leaves_other_work_chapters_memory_and_story_bible_untouched(self):
        chapter_id = self.chapter().id
        other_nid = repos.NovelRepo(self.db).create(title="作品B").id
        other_chapter = repos.ChapterRepo(self.db).create(
            other_nid, title="B章", content="B正文", status="finalized", summary="B摘要", tension_score=6.2, drift_score=0.1,
        )
        other_id = other_chapter.id
        repos.MemoryChunkRepo(self.db).create(other_nid, kind="chapter_slice", source_chapter_id=other_id, text="B正文", vector=[1.0])
        # 即使旧库有另一作品错误引用目标章，作品A保存也不能修改作品B数据。
        repos.MemoryChunkRepo(self.db).create(other_nid, kind="chapter_slice", source_chapter_id=chapter_id, text="B旧数据", vector=[1.0])
        char_a = repos.CharacterRepo(self.db).create(self.nid, name="A人物")
        char_a_id = char_a.id
        fs_a = repos.ForeshadowingRepo(self.db).create(self.nid, content="A伏笔", planted_chapter_id=chapter_id)
        fs_a_id = fs_a.id
        char_b = repos.CharacterRepo(self.db).create(other_nid, name="B人物")
        char_b_id = char_b.id
        response = self.client.put(f"/api/v1/novels/{self.nid}/chapters/{chapter_id}", json={"content": "A新正文"})
        self.assertEqual(response.status_code, 200, response.text)
        self.db.expire_all()
        other = repos.ChapterRepo(self.db).get(other_nid, other_id)
        self.assertEqual((other.content, other.status, other.summary, other.tension_score, other.drift_score),
                         ("B正文", "finalized", "B摘要", 6.2, 0.1))
        self.assertEqual({chunk.text for chunk in repos.MemoryChunkRepo(self.db).list_by_novel(other_nid)}, {"B正文", "B旧数据"})
        self.assertEqual(repos.CharacterRepo(self.db).get(self.nid, char_a_id).name, "A人物")
        self.assertEqual(repos.ForeshadowingRepo(self.db).get(self.nid, fs_a_id).planted_chapter_id, chapter_id)
        self.assertEqual(repos.CharacterRepo(self.db).get(other_nid, char_b_id).name, "B人物")
        self.assertEqual(self.db.query(models.ChapterRevision).filter_by(novel_id=other_nid).count(), 0)

    def test_clean_session_guard_preserves_callers_uncommitted_extra_changes(self):
        chapter_id = self.chapter().id
        pending = models.Character(novel_id=self.nid, name="调用方未提交人物")
        self.db.add(pending)
        with self.assertRaises(RuntimeError):
            revisions.update_chapter_with_revision(self.db, self.nid, chapter_id, {"content": "拒绝覆盖"}, revisions.SOURCE_MANUAL_SAVE)
        self.assertIn(pending, self.db.new)
        self.db.rollback()

    def test_continue_and_direct_rewrite_success_invalidate_old_derived_state(self):
        for mode in ("continue", "rewrite"):
            with self.subTest(mode=mode):
                chapter_id = self.chapter().id
                with patch.object(writing.llm, "stream_complete", return_value=iter(["改写段落"])):
                    stream = writing.continue_chapter(self.db, self.nid, chapter_id) if mode == "continue" else writing.rewrite_passage(self.db, self.nid, chapter_id, "死于北城")
                    result = events(stream)
                done = next(event for event in result if event["type"] == "done")
                self.assertEqual((done["chapter"]["status"], done["chapter"]["summary"], done["chapter"]["tension_score"]),
                                 ("draft", "", None))
                self.assertEqual(self.rows(chapter_id)[0].status, "finalized")


class RevisionConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="lingmo-author-race-")
        self.engine = create_engine(f"sqlite:///{(Path(self.directory.name) / 'race.db').as_posix()}", connect_args={"check_same_thread": False})
        models.Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        with self.Session() as db:
            self.nid = repos.NovelRepo(db).create(title="并发写作作品").id
            self.cid = repos.ChapterRepo(db).create(self.nid, title="原标题", content="原稿", directive="原指令").id

    def tearDown(self):
        self.engine.dispose()
        self.directory.cleanup()

    def test_overlapping_manual_saves_allocate_distinct_versions_and_preserve_every_overwritten_body(self):
        barrier = Barrier(2)
        errors = []

        def save(body):
            with self.Session() as db:
                # 两个真实请求先各自读到旧稿；写入时必须重读，不能按旧ORM快照。
                stale = repos.ChapterRepo(db).get(self.nid, self.cid)
                barrier.wait(5)
                try:
                    revisions.update_chapter_with_revision(db, self.nid, self.cid, {"content": body}, revisions.SOURCE_MANUAL_SAVE)
                except Exception as error:
                    errors.append(error)
                self.assertIsNotNone(stale)

        writers = [Thread(target=save, args=(body,), daemon=True) for body in ("作者编辑甲", "作者编辑乙")]
        for writer in writers:
            writer.start()
        for writer in writers:
            writer.join(8)
        self.assertTrue(all(not writer.is_alive() for writer in writers))
        self.assertEqual(errors, [])
        with self.Session() as db:
            current = repos.ChapterRepo(db).get(self.nid, self.cid)
            versions = sorted(repos.ChapterRevisionRepo(db).list_by_chapter(self.nid, self.cid), key=lambda item: item.revision_no)
            self.assertEqual([version.revision_no for version in versions], [1, 2])
            self.assertEqual(versions[0].content, "原稿")
            self.assertEqual({versions[1].content, current.content}, {"作者编辑甲", "作者编辑乙"})

    def test_streaming_wait_does_not_lock_manual_save_and_late_write_conflicts_preserve_tokens(self):
        for mode in ("continue", "rewrite"):
            for field, value in (("content", "作者新保存稿"), ("title", "作者新标题"), ("directive", "作者新指令")):
                with self.subTest(mode=mode, field=field), self.Session() as db:
                    chapter_id = repos.ChapterRepo(db).create(self.nid, title="原标题", content="原稿", directive="原指令").id
                    started, saved = Event(), Event()
                    errors = []

                    def author_save():
                        try:
                            if not started.wait(5):
                                raise AssertionError("生成未开始")
                            with self.Session() as author:
                                revisions.update_chapter_with_revision(author, self.nid, chapter_id, {field: value}, revisions.SOURCE_MANUAL_SAVE)
                        except Exception as error:
                            errors.append(error)
                        finally:
                            saved.set()

                    def model_stream(*args, **kwargs):
                        started.set()
                        if not saved.wait(5) or errors:
                            raise AssertionError(f"模型等待阻塞了手工保存：{errors}")
                        yield "完整模型结果"

                    writer = Thread(target=author_save, daemon=True)
                    writer.start()
                    try:
                        with patch.object(writing.llm, "stream_complete", side_effect=model_stream):
                            stream = writing.continue_chapter(db, self.nid, chapter_id) if mode == "continue" else writing.rewrite_passage(db, self.nid, chapter_id, "原稿")
                            result = events(stream)
                    finally:
                        writer.join(8)
                    self.assertFalse(writer.is_alive())
                    self.assertEqual(errors, [])
                    self.assertEqual([event["type"] for event in result], ["token", "error"])
                    self.assertEqual(result[0]["content"], "完整模型结果")
                    self.assertEqual((result[-1]["status"], result[-1]["code"]), (409, "chapter_write_conflict"))
                    db.expire_all()
                    current = repos.ChapterRepo(db).get(self.nid, chapter_id)
                    self.assertEqual(getattr(current, field), value)
                    versions = repos.ChapterRevisionRepo(db).list_by_chapter(self.nid, chapter_id)
                    self.assertEqual([(version.revision_no, version.content) for version in versions], [(1, "原稿")])

    def test_flushed_and_core_caller_writes_are_refused_without_losing_caller_transaction(self):
        for action in ("update", "restore"):
            for pending_kind in ("flushed_insert", "core_update"):
                with self.subTest(action=action, pending_kind=pending_kind), self.Session() as db:
                    chapter_id = repos.ChapterRepo(db).create(self.nid, title="事务边界章", content="原稿").id
                    revision_id = None
                    expected = "原稿"
                    if action == "restore":
                        revisions.update_chapter_with_revision(db, self.nid, chapter_id, {"content": "恢复前稿"}, revisions.SOURCE_MANUAL_SAVE)
                        revision_id = repos.ChapterRevisionRepo(db).list_by_chapter(self.nid, chapter_id)[0].id
                        expected = "恢复前稿"
                    if pending_kind == "flushed_insert":
                        pending = models.Character(novel_id=self.nid, name=f"调用方人物{action}")
                        db.add(pending)
                        db.flush()
                        pending_id = pending.id
                    else:
                        db.execute(update(models.Novel).where(models.Novel.id == self.nid).values(title=f"调用方新作品名{action}"))
                    self.assertEqual((len(db.new), len(db.dirty), len(db.deleted)), (0, 0, 0))
                    self.assertTrue(db.connection().connection.driver_connection.in_transaction)
                    with self.assertRaises(RuntimeError):
                        if action == "update":
                            revisions.update_chapter_with_revision(db, self.nid, chapter_id, {"content": "应被拒绝的新稿"}, revisions.SOURCE_MANUAL_SAVE)
                        else:
                            revisions.restore_chapter_revision(db, self.nid, chapter_id, revision_id)
                    self.assertTrue(db.connection().connection.driver_connection.in_transaction)
                    db.commit()  # 调用方仍能自行提交它此前的写入
                    with self.Session() as verify:
                        self.assertEqual(repos.ChapterRepo(verify).get(self.nid, chapter_id).content, expected)
                        self.assertEqual(len(repos.ChapterRevisionRepo(verify).list_by_chapter(self.nid, chapter_id)), int(action == "restore"))
                        if pending_kind == "flushed_insert":
                            self.assertIsNotNone(verify.get(models.Character, pending_id))
                        else:
                            self.assertEqual(repos.NovelRepo(verify).get(self.nid).title, f"调用方新作品名{action}")

    def test_readonly_autobegin_remains_allowed_and_returns_same_loaded_identity(self):
        with self.Session() as db:
            original = repos.ChapterRepo(db).get(self.nid, self.cid)
            self.assertTrue(db.in_transaction())
            self.assertFalse(db.connection().connection.driver_connection.in_transaction)
            saved = revisions.update_chapter_with_revision(db, self.nid, self.cid, {"content": "只读后正常保存"}, revisions.SOURCE_MANUAL_SAVE)
            self.assertIs(saved, original)
            self.assertIs(db.get(models.Chapter, self.cid), saved)
            self.assertFalse(inspect(saved).expired)
            self.assertEqual(list(db.dirty), [])
            self.assertEqual(saved.content, "只读后正常保存")

    def test_real_exclusive_lock_after_commit_does_not_turn_saved_results_into_conflicts(self):
        path = Path(self.directory.name) / "race.db"
        timed_engine = create_engine(f"sqlite:///{path.as_posix()}", connect_args={"check_same_thread": False, "timeout": 0.02})
        TimedSession = sessionmaker(bind=timed_engine, autoflush=False)
        try:
            for mode in ("update", "restore", "continue", "rewrite"):
                with self.subTest(mode=mode), TimedSession() as db:
                    chapter_id = repos.ChapterRepo(db).create(self.nid, title="锁验证章", content="原稿").id
                    revision_id = None
                    if mode == "restore":
                        revisions.update_chapter_with_revision(db, self.nid, chapter_id, {"content": "恢复前稿"}, revisions.SOURCE_MANUAL_SAVE)
                        revision_id = repos.ChapterRevisionRepo(db).list_by_chapter(self.nid, chapter_id)[0].id
                    original = repos.ChapterRepo(db).get(self.nid, chapter_id)
                    real_commit = db.commit
                    blocker = sqlite3.connect(path, timeout=0.02, isolation_level=None)

                    def commit_then_lock():
                        real_commit()
                        blocker.execute("BEGIN EXCLUSIVE")  # 真实提交之后，任意后置SELECT都会因锁失败

                    try:
                        with (
                            patch.object(db, "commit", side_effect=commit_then_lock),
                            patch.object(writing.llm, "stream_complete", return_value=iter(["模型文本"])),
                        ):
                            if mode == "update":
                                saved = revisions.update_chapter_with_revision(db, self.nid, chapter_id, {"content": "已提交新稿"}, revisions.SOURCE_MANUAL_SAVE)
                            elif mode == "restore":
                                saved = revisions.restore_chapter_revision(db, self.nid, chapter_id, revision_id)
                            else:
                                stream = writing.continue_chapter(db, self.nid, chapter_id) if mode == "continue" else writing.rewrite_passage(db, self.nid, chapter_id, "原稿")
                                result = events(stream)
                                self.assertEqual([event["type"] for event in result], ["token", "done"])
                                saved = original
                                self.assertEqual(result[-1]["chapter"], writing._chapter_dict(saved))
                            # API序列化所需的所有列、身份读取也必须能在真实排他锁尚未解除时完成。
                            frozen = writing._chapter_dict(saved)
                            self.assertIs(saved, original)
                            self.assertIs(db.get(models.Chapter, chapter_id), saved)
                            self.assertFalse(inspect(saved).expired)
                            self.assertEqual(list(db.dirty), [])
                    finally:
                        blocker.rollback()
                        blocker.close()
                    with self.Session() as verify:
                        persisted = repos.ChapterRepo(verify).get(self.nid, chapter_id)
                        self.assertEqual(writing._chapter_dict(persisted), frozen)
                        expected = {"update": "已提交新稿", "restore": "原稿", "continue": "原稿模型文本", "rewrite": "模型文本"}[mode]
                        self.assertEqual(persisted.content, expected)
                        self.assertEqual(len(repos.ChapterRevisionRepo(verify).list_by_chapter(self.nid, chapter_id)), 2 if mode == "restore" else 1)
        finally:
            timed_engine.dispose()
