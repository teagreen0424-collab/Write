"""流式中断的持久化边界：不建章、不覆盖正文、不制造版本或记忆。"""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.application import auto_pilot, writing
from app.infrastructure import models
from app.infrastructure.ai.llm_provider import LLMStreamIncompleteError
from app.infrastructure import repository as repos
from app.main import app


def events(response):
    return [json.loads(line[5:].strip()) for line in response.text.splitlines()
            if line.startswith("data:")]


def truncated_stream(*args, **kwargs):
    yield "这是已经收到的未完成稿。"
    yield "最后半句话"
    raise LLMStreamIncompleteError(
        "输出达到长度上限，生成未完成。", code="output_truncated", finish_reason="length"
    )


class StreamPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        models.Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.session_factory()
        self.client = TestClient(app)
        self.novel_id = repos.NovelRepo(self.db).create(title="流式边界测试").id
        self.chapter = repos.ChapterRepo(self.db).create(
            self.novel_id, seq=1, title="原章", content="开头。待改写段落。完整结尾。",
            directive="保留原章", status="finalized", summary="原摘要",
            tension_score=7.0, drift_score=0.4,
        )
        repos.CharacterRepo(self.db).create(self.novel_id, name="原人物", known_info="原因未知")
        repos.MemoryChunkRepo(self.db).create(
            self.novel_id, kind="chapter_slice", source_chapter_id=self.chapter.id,
            text="原章节记忆", embedding_id="", vector=[],
        )
        self.context_patch = patch("app.application.writing.build_context", return_value="设定上下文")
        self.context_patch.start()
        self.session_patch = patch(
            "app.interfaces.api.v1.chapters.SessionLocal", side_effect=self.session_factory
        )
        self.session_patch.start()
        self.before = self.rows()

    def tearDown(self):
        self.session_patch.stop()
        self.context_patch.stop()
        self.db.close()
        self.engine.dispose()

    def rows(self):
        self.db.expire_all()
        return {
            table.name: sorted(
                [dict(row) for row in self.db.execute(select(table)).mappings()],
                key=lambda row: row["id"],
            )
            for table in models.Base.metadata.sorted_tables
        }

    def assert_failed_without_writes(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        emitted = events(response)
        self.assertEqual([event["type"] for event in emitted], ["token", "token", "error"])
        self.assertEqual(emitted[-1]["code"], "output_truncated")
        self.assertEqual("".join(event.get("content", "") for event in emitted),
                         "这是已经收到的未完成稿。最后半句话")
        self.assertEqual(self.rows(), self.before)

    def test_truncated_new_chapter_never_inserts_or_marks_success(self):
        with patch.object(writing.llm, "stream_complete", side_effect=truncated_stream):
            response = self.client.post(
                f"/api/v1/novels/{self.novel_id}/chapters/write",
                json={"title": "不应落库的章节", "previous_chapter_id": self.chapter.id},
            )
        self.assert_failed_without_writes(response)

    def test_truncated_continue_preserves_finalized_body_and_derived_data(self):
        with patch.object(writing.llm, "stream_complete", side_effect=truncated_stream):
            response = self.client.post(
                f"/api/v1/novels/{self.novel_id}/chapters/{self.chapter.id}/continue",
                json={"directive": "继续"},
            )
        self.assert_failed_without_writes(response)

    def test_truncated_rewrite_cannot_apply_or_create_preview(self):
        for preview in (False, True):
            with self.subTest(preview=preview), patch.object(
                writing.llm, "stream_complete", side_effect=truncated_stream
            ):
                response = self.client.post(
                    f"/api/v1/novels/{self.novel_id}/chapters/{self.chapter.id}/rewrite",
                    json={"passage": "待改写段落", "intent": "去AI味", "preview": preview},
                )
                self.assert_failed_without_writes(response)

    def test_empty_success_is_rejected_before_any_write(self):
        with patch.object(writing.llm, "stream_complete", return_value=iter(["  \n"])):
            response = self.client.post(
                f"/api/v1/novels/{self.novel_id}/chapters/write", json={"title": "空章"}
            )
        self.assertEqual(events(response)[-1]["type"], "error")
        self.assertFalse(any(event["type"] == "done" for event in events(response)))
        self.assertEqual(self.rows(), self.before)

    def test_new_chapter_budget_is_separate_and_normal_completion_still_persists(self):
        with (
            patch("app.application.writing.get_settings", return_value=SimpleNamespace(writing_max_tokens=6144)),
            patch.object(writing.llm, "stream_complete", return_value=iter(["完整新章。"])) as stream,
        ):
            response = self.client.post(
                f"/api/v1/novels/{self.novel_id}/chapters/write", json={"title": "完整新章"}
            )
        self.assertEqual(stream.call_args.kwargs["max_tokens"], 6144)
        emitted = events(response)
        self.assertEqual(emitted[-1]["type"], "done")
        self.assertEqual(emitted[-1]["chapter"]["status"], "draft")
        self.assertEqual(emitted[-1]["chapter"]["content"], "完整新章。")
        self.db.expire_all()
        self.assertEqual(repos.ChapterRepo(self.db).get(self.novel_id, self.chapter.id).content,
                         "开头。待改写段落。完整结尾。")
        self.assertEqual(len(repos.ChapterRepo(self.db).list(self.novel_id)), 2)
        self.assertEqual(self.rows()["chapter_revision"], self.before["chapter_revision"])

    def test_truncated_autopilot_never_creates_chapter_or_reports_full_completion(self):
        repos.OutlineRepo(self.db).create(
            self.novel_id, title="第二章", level="chapter", summary="新一章剧情"
        )
        self.before = self.rows()
        with (
            patch.object(auto_pilot.llm, "stream_complete", side_effect=truncated_stream),
            patch("app.application.auto_pilot.build_context", return_value="设定"),
        ):
            emitted = [json.loads(frame[5:].strip()) for frame in auto_pilot.run_autopilot(
                self.novel_id, session_factory=self.session_factory
            )]
        self.assertTrue(any(event["type"] == "error" for event in emitted))
        self.assertFalse(any(event["type"] == "chapter_done" for event in emitted))
        self.assertEqual(emitted[-1]["done"], 0)
        self.assertIn("未完成", emitted[-1]["message"])
        self.assertEqual(self.rows(), self.before)
        self.assertFalse(auto_pilot.registry.is_running(self.novel_id))


if __name__ == "__main__":
    unittest.main()
