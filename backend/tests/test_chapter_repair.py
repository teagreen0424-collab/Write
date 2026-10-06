"""整章修订的原稿保护、真实计数及原子应用；只使用隔离SQLite和模型桩。"""
import json
import tempfile
import unittest
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import sessionmaker

from app.application import chapter_repair as repair
from app.infrastructure import models, repository as repos
from app.infrastructure.db import get_db
from app.interfaces.api.v1 import chapters
from app.interfaces.schemas import ChapterRepairApplyRequest, ChapterRepairPreviewRequest, StoryCheckIssue


class ChapterRepairTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        path = (Path(self.directory.name) / "repair.db").as_posix()
        self.engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
        models.Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.sessions()
        self.novel = repos.NovelRepo(self.db).create(title="修订验证")
        self.character = repos.CharacterRepo(self.db).create(self.novel.id, name="甲", known_info="左手旧伤不能承重")
        self.outline = repos.OutlineRepo(self.db).create(self.novel.id, title="公开副本", level="chapter", summary="依法取得公开副本")
        self.prior = repos.ChapterRepo(self.db).create(self.novel.id, seq=1, title="前章", content="两人约定次日下午见面。")
        self.original = "甲拒绝偷取档案。\n乙说明登记手续，两人依法取得公开副本。 汉字🙂"
        self.chapter = repos.ChapterRepo(self.db).create(
            self.novel.id, seq=2, title="本章", outline_node_id=self.outline.id, content=self.original,
            directive="有人提议偷取档案，甲拒绝；不改变左手旧伤。",
            status="finalized", summary="旧摘要", tension_score=6.5, drift_score=0.3,
        )
        self.memory = repos.MemoryChunkRepo(self.db).create(
            self.novel.id, kind="chapter_slice", source_chapter_id=self.chapter.id, text="旧正文记忆", vector=[], embedding_id="",
        )
        self.other_memory = repos.MemoryChunkRepo(self.db).create(
            self.novel.id, kind="chapter_slice", source_chapter_id=self.prior.id, text="前章记忆", vector=[], embedding_id="",
        )
        self.provider = Mock()
        self.provider.complete.return_value = "乙提出偷取档案。甲拒绝。乙说明登记手续，两人依法取得公开副本。"
        app = FastAPI()
        app.include_router(chapters.router, prefix="/api/v1")
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)
        self.path = f"/api/v1/novels/{self.novel.id}/chapters/{self.chapter.id}"

    def tearDown(self):
        self.client.close()
        self.db.close()
        self.engine.dispose()
        self.directory.cleanup()

    def snapshot(self):
        return {table.name: [tuple(row) for row in self.db.execute(select(table)).all()]
                for table in models.Base.metadata.sorted_tables}

    def preview(self, **kwargs):
        return repair.preview_repair(
            self.db, self.novel.id, self.chapter.id, ChapterRepairPreviewRequest(**kwargs), provider=self.provider,
        )

    def apply(self, preview, content=None):
        return repair.apply_repair(self.db, self.novel.id, self.chapter.id, ChapterRepairApplyRequest(
            base_fingerprint=preview.base_fingerprint, content=preview.content if content is None else content,
        ))

    def fresh(self):
        self.db.expire_all()
        return repos.ChapterRepo(self.db).get(self.novel.id, self.chapter.id)

    def test_preview_is_single_call_read_only_and_preserves_full_unicode_source(self):
        before = self.snapshot()
        with patch.object(repair, "get_settings", return_value=SimpleNamespace(writing_max_tokens=10240)):
            result = self.preview(word_limits={"min_words": 12, "max_words": 20})
        self.provider.complete.assert_called_once()
        self.assertEqual(result.original_content, self.original)
        self.assertEqual(result.original_word_count.count, sum(not ch.isspace() for ch in self.original))
        self.assertEqual(result.word_count.count, sum(not ch.isspace() for ch in result.content))
        self.assertFalse(result.original_word_count.within_limits)
        self.assertNotIn("已保存", "".join(result.word_count.warnings))
        self.assertEqual(self.provider.complete.call_args.kwargs["max_tokens"], 10240)
        self.assertTrue(self.provider.complete.call_args.kwargs["require_complete"])
        prompt = self.provider.complete.call_args.args[0][1]["content"]
        data = json.JSONDecoder().raw_decode(prompt[prompt.index("{"):])[0]
        self.assertEqual(data["chapter"]["content"], self.original)
        self.assertEqual(data["前章资料（草稿及摘要可能有错，不覆盖设定）"][0]["content"], self.prior.content)
        self.assertEqual(data["Story Bible（唯一权威设定）"]["人物"][0]["known_info"], self.character.known_info)
        self.assertEqual(data["程序计算的完整正文字数"]["count"], result.original_word_count.count)
        self.assertEqual(self.snapshot(), before)

    def test_reports_and_pending_ids_are_hints_and_preserve_real_requirements(self):
        issue = StoryCheckIssue(category="directive", severity="high", requirement="有人提议偷取档案",
                                reason="原稿未出现提议", excerpt="")
        result = self.preview(instructions="删去重复感想，保留提议和拒绝场景", issues=[issue], unreviewed_requirement_ids=["d002", "d002"])
        messages = self.provider.complete.call_args.args[0]
        self.assertIn("待核对ID只是提醒验证", messages[0]["content"])
        self.assertIn("不是新事实", messages[0]["content"])
        data = json.JSONDecoder().raw_decode(messages[1]["content"][messages[1]["content"].index("{"):])[0]
        self.assertEqual(data["待核对要求ID（只验证，不代表违规）"], ["d002"])
        self.assertEqual(data["AI核对提示（可能有误，须核实，不是新增事实或要求）"][0]["reason"], "原稿未出现提议")
        self.assertIn("不改变左手旧伤", [item["text"] for item in data["作者原文逐项要求"]])
        self.assertEqual(result.directive, self.chapter.directive)

    def test_input_over_limit_rejected_before_call_without_truncating_or_writing(self):
        repos.ChapterRepo(self.db).update(self.novel.id, self.chapter.id, content="中" * (repair.MAX_ORIGINAL_CHARS + 1))
        before = self.snapshot()
        with self.assertRaises(HTTPException) as caught:
            self.preview()
        self.assertEqual(caught.exception.status_code, 413)
        self.provider.complete.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_context_over_limit_rejected_without_shortening_original(self):
        repos.ChapterRepo(self.db).update(self.novel.id, self.prior.id, content="前" * repair.MAX_MATERIALS_CHARS)
        before = self.snapshot()
        with self.assertRaises(HTTPException) as caught:
            self.preview()
        self.assertEqual(caught.exception.status_code, 413)
        self.provider.complete.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_invalid_model_output_and_private_error_are_safe_and_read_only(self):
        before = self.snapshot()
        for output in ("", "\n\t", None, "```\n正文\n```", "字" * (repair.MAX_REPAIR_CHARS + 1)):
            with self.subTest(output_type=type(output).__name__):
                self.provider.complete.return_value = output
                with self.assertRaises(HTTPException) as caught:
                    self.preview()
                self.assertEqual(caught.exception.status_code, 502)
                self.assertEqual(self.snapshot(), before)
        self.provider.complete.side_effect = RuntimeError("https://private.invalid/api SECRET_KEY")
        with self.assertRaises(HTTPException) as caught:
            self.preview()
        self.assertEqual(caught.exception.status_code, 502)
        self.assertNotIn("private", caught.exception.detail)
        self.assertNotIn("SECRET", caught.exception.detail)
        self.assertEqual(self.snapshot(), before)

    def test_unknown_pending_id_rejected_before_model(self):
        with self.assertRaises(HTTPException) as caught:
            self.preview(unreviewed_requirement_ids=["d999"])
        self.assertEqual(caught.exception.status_code, 400)
        self.provider.complete.assert_not_called()

    def test_apply_keeps_exact_old_version_invalidates_derived_data_only(self):
        preview = self.preview()
        bible_before = self.snapshot()["character"]
        saved = self.apply(preview)
        self.assertEqual(saved.content, preview.content)
        self.assertEqual(saved.status, "draft")
        self.assertEqual(saved.summary, "")
        self.assertIsNone(saved.tension_score)
        self.assertIsNone(saved.drift_score)
        self.assertEqual((saved.title, saved.directive, saved.seq, saved.outline_node_id),
                         (preview.title, preview.directive, 2, self.outline.id))
        versions = repos.ChapterRevisionRepo(self.db).list_by_chapter(self.novel.id, self.chapter.id)
        self.assertEqual(len(versions), 1)
        old = versions[0]
        self.assertEqual((old.source, old.content, old.status, old.summary, old.tension_score, old.drift_score),
                         ("rewrite_apply", self.original, "finalized", "旧摘要", 6.5, 0.3))
        self.assertEqual(self.db.query(models.MemoryChunk).filter_by(source_chapter_id=self.chapter.id).count(), 0)
        self.assertEqual(self.db.query(models.MemoryChunk).filter_by(source_chapter_id=self.prior.id).count(), 1)
        self.assertEqual(self.snapshot()["character"], bible_before)

    def test_same_content_application_does_not_make_revision_or_clear_saved_state(self):
        preview = self.preview()
        before = self.snapshot()
        saved = self.apply(preview, content=self.original)
        self.assertEqual(saved.status, "finalized")
        self.assertEqual(self.snapshot(), before)

    def test_old_preview_cannot_overwrite_new_author_content(self):
        preview = self.preview()
        repos.ChapterRepo(self.db).update(self.novel.id, self.chapter.id, content="作者刚保存的新稿")
        before = self.snapshot()
        with self.assertRaises(HTTPException) as caught:
            self.apply(preview)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.snapshot(), before)

    def test_independent_session_change_is_not_hidden_by_cached_author_row(self):
        preview = self.preview()
        self.assertEqual(self.fresh().content, self.original)
        with self.sessions() as writer:
            repos.ChapterRepo(writer).update(self.novel.id, self.chapter.id, content="另一个窗口的新稿")
        with self.assertRaises(HTTPException) as caught:
            self.apply(preview)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.fresh().content, "另一个窗口的新稿")
        self.assertEqual(repos.ChapterRevisionRepo(self.db).list_by_chapter(self.novel.id, self.chapter.id), [])

    def test_context_or_status_changes_invalidate_preview(self):
        mutations = (
            lambda: repos.CharacterRepo(self.db).update(self.novel.id, self.character.id, known_info="新设定"),
            lambda: repos.OutlineRepo(self.db).update(self.novel.id, self.outline.id, summary="新的大纲要求"),
            lambda: repos.ChapterRepo(self.db).update(self.novel.id, self.prior.id, content="前章刚修改"),
            lambda: repos.ChapterRepo(self.db).update(self.novel.id, self.chapter.id, status="draft"),
            lambda: repos.ChapterRepo(self.db).update(self.novel.id, self.chapter.id, summary="新的摘要"),
            lambda: repos.ChapterRepo(self.db).update(self.novel.id, self.chapter.id, outline_node_id=""),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                preview = self.preview()
                mutation()
                before = self.snapshot()
                with self.assertRaises(HTTPException) as caught:
                    self.apply(preview)
                self.assertEqual(caught.exception.status_code, 409)
                self.assertEqual(self.snapshot(), before)

    def test_author_aba_change_is_rejected_by_timestamp(self):
        preview = self.preview()
        repos.ChapterRepo(self.db).update(self.novel.id, self.chapter.id, content="临时稿")
        repos.ChapterRepo(self.db).update(self.novel.id, self.chapter.id, content=self.original)
        before = self.snapshot()
        with self.assertRaises(HTTPException) as caught:
            self.apply(preview)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.snapshot(), before)

    def test_cas_guard_rejects_row_changed_after_context_read(self):
        preview = self.preview()
        before = self.snapshot()
        actual = repair._base_materials
        def read_then_change(*args):
            result = actual(*args)
            self.db.execute(update(models.Chapter).where(models.Chapter.id == self.chapter.id)
                            .values(content="模拟校验后变更").execution_options(synchronize_session=False))
            return result
        with patch.object(repair, "_base_materials", side_effect=read_then_change):
            with self.assertRaises(HTTPException) as caught:
                self.apply(preview)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.snapshot(), before)

    def test_sqlite_lock_blocks_independent_context_writer_until_atomic_apply_finishes(self):
        preview = self.preview()
        novel_id, character_id = self.novel.id, self.character.id
        start, attempted, finished = Event(), Event(), Event()
        errors = []
        def writer():
            if not start.wait(3):
                return
            try:
                with self.sessions() as separate:
                    separate.execute(update(models.Character).where(models.Character.id == character_id)
                                     .values(known_info="应用完成后作者更新设定"))
                    separate.commit()
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()
        def observe(_conn, _cursor, statement, _params, _context, _many):
            if statement.lstrip().upper().startswith("UPDATE CHARACTER "):
                attempted.set()
        actual = repair._base_materials
        def read_then_attempt_context_write(*args):
            result = actual(*args)
            start.set()
            self.assertTrue(attempted.wait(3), "独立writer未尝试写入")
            self.assertFalse(finished.wait(0.1), "独立设定写入穿过了校验与CAS间的SQLite写锁")
            return result
        event.listen(self.engine, "before_cursor_execute", observe)
        worker = Thread(target=writer, daemon=True)
        worker.start()
        try:
            with patch.object(repair, "_base_materials", side_effect=read_then_attempt_context_write):
                saved = self.apply(preview)
            self.assertTrue(finished.wait(3), "应用commit后writer仍未完成")
            self.assertEqual(errors, [])
            self.assertEqual(saved.content, preview.content)
            self.db.expire_all()
            self.assertEqual(repos.CharacterRepo(self.db).get(novel_id, character_id).known_info, "应用完成后作者更新设定")
            self.assertEqual(len(repos.ChapterRevisionRepo(self.db).list_by_chapter(novel_id, self.chapter.id)), 1)
        finally:
            start.set()
            worker.join(3)
            event.remove(self.engine, "before_cursor_execute", observe)

    def test_snapshot_or_memory_cleanup_failure_rolls_back_cas_and_version(self):
        for function in ("snapshot_chapter", "delete_chapter_memory"):
            with self.subTest(function=function):
                preview = self.preview()
                before = self.snapshot()
                with patch.object(repair, function, side_effect=RuntimeError("故障")):
                    with self.assertRaises(RuntimeError):
                        self.apply(preview)
                self.assertEqual(self.snapshot(), before)

    def test_api_validation_scope_and_empty_application_do_not_write(self):
        before = self.snapshot()
        with patch.object(repair.llm, "complete") as complete:
            for payload in ({"instructions": "x" * 4001}, {"word_limits": {}}, {"word_limits": {"min_words": True}},
                            {"word_limits": {"min_words": 10, "max_words": 2}}, {"unexpected": 1}):
                response = self.client.post(self.path + "/repair-preview", json=payload)
                self.assertEqual(response.status_code, 422, response.text)
            complete.assert_not_called()
        foreign = repos.NovelRepo(self.db).create(title="其他作品")
        scoped_before = self.snapshot()
        preview = self.preview()
        for operation, payload in (("repair-preview", {}), ("repair-apply", {"base_fingerprint": preview.base_fingerprint, "content": "修订稿"})):
            response = self.client.post(f"/api/v1/novels/{foreign.id}/chapters/{self.chapter.id}/{operation}", json=payload)
            self.assertEqual(response.status_code, 404)
        self.assertEqual(self.snapshot(), scoped_before)
        for body, expected in (("", 422), ("\n\t ", 400)):
            response = self.client.post(self.path + "/repair-apply", json={"base_fingerprint": preview.base_fingerprint, "content": body})
            self.assertEqual(response.status_code, expected)
        self.assertEqual(self.snapshot(), scoped_before)

    def test_api_preview_and_apply_contract_and_replay_conflict(self):
        with patch.object(repair.llm, "complete", return_value=self.provider.complete.return_value) as complete:
            response = self.client.post(self.path + "/repair-preview", json={"word_limits": {"max_words": 20}})
        self.assertEqual(response.status_code, 200, response.text)
        complete.assert_called_once()
        payload = response.json()
        self.assertEqual(set(payload), {"chapter_id", "base_fingerprint", "original_content", "content", "title", "directive", "original_word_count", "word_count"})
        apply = {"base_fingerprint": payload["base_fingerprint"], "content": payload["content"]}
        response = self.client.post(self.path + "/repair-apply", json=apply)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["content"], apply["content"])
        before = self.snapshot()
        response = self.client.post(self.path + "/repair-apply", json=apply)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
