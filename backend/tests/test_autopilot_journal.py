"""真实 SQLite 文件验收：已发送片段先持久化，刷新恢复与正文事务隔离。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.application import auto_pilot
from app.infrastructure import models
from app.infrastructure.autopilot_drafts import AutopilotDraftRepo
from app.infrastructure.ai.llm_provider import LLMStreamIncompleteError
from app.infrastructure import repository as repos
from app.interfaces.schemas import StoryCheckResult


def _event(frame):
    return json.loads(frame[5:].strip())


def _truncated():
    yield "失败时收到的第一段。"
    yield "末尾仍有半句话"
    raise LLMStreamIncompleteError("达到输出上限", code="output_truncated", finish_reason="length")


class AutopilotJournalTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = (Path(directory.name) / "recoverable-stream.db").as_posix()
        self.engine = create_engine(
            f"sqlite:///{path}", connect_args={"check_same_thread": False},
        )
        models.Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.sessions()
        self.addCleanup(self.db.close)
        self.novel_id = repos.NovelRepo(self.db).create(title="逐片恢复验收").id
        for mocked in (
            patch.object(auto_pilot, "registry", auto_pilot.AutoPilotRegistry()),
            patch.object(auto_pilot, "build_context", return_value="不调用外部服务的上下文"),
            patch.object(auto_pilot, "review_story", return_value=StoryCheckResult()),
            patch.object(auto_pilot, "consistency_check", return_value={"conflicts": []}),
            patch.object(auto_pilot, "score_tension", return_value=5.0),
            patch.object(auto_pilot, "check_drift", return_value=None),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)

    def _nodes(self, count):
        return [repos.OutlineRepo(self.db).create(
            self.novel_id, title=f"第{index}章", level="chapter", sort_order=index,
        ) for index in range(1, count + 1)]

    def _run(self):
        return [_event(frame) for frame in auto_pilot.run_autopilot(
            self.novel_id, session_factory=self.sessions,
        )]

    def _rows(self):
        # 独立连接是验收重点：不能只看到生成线程自身未提交的内存态。
        with self.sessions() as observer:
            return {
                table.name: sorted(
                    [dict(row) for row in observer.execute(select(table)).mappings()],
                    key=lambda row: row["id"],
                ) for table in models.Base.metadata.sorted_tables
            }

    def _drafts(self):
        with self.sessions() as observer:
            return AutopilotDraftRepo(observer).list(self.novel_id)

    def _sql(self, statement):
        with self.engine.begin() as conn:
            conn.execute(text(statement))

    def test_every_sent_text_is_committed_and_visible_to_fresh_sqlite_connection(self):
        self._nodes(1)
        received = ""
        prior_revision = 0
        text_count = 0
        stream = auto_pilot.run_autopilot(self.novel_id, session_factory=self.sessions)
        with patch.object(auto_pilot.llm, "stream_complete", return_value=iter(["中文第一段。", "\n末尾内容。"] )):
            emitted = []
            for frame in stream:
                event = _event(frame)
                emitted.append(event)
                if event["type"] == "text":
                    text_count += 1
                    received += event["content"]
                    self.assertTrue(event["draft_persisted"])
                    self.assertGreater(event["draft_revision"], prior_revision)
                    prior_revision = event["draft_revision"]
                    with self.sessions() as observer:
                        saved = AutopilotDraftRepo(observer).get_raw(self.novel_id, event["draft_id"])
                        self.assertEqual((saved.content, saved.revision), (received, event["draft_revision"]))
                        self.assertEqual(saved.state, "active")
                        self.assertEqual(observer.query(models.Chapter).count(), 0)
                elif event["type"] == "chapter_done":
                    rows = self._rows()
                    self.assertEqual(rows["chapter"][0]["content"], received)
                    cache = rows["autopilot_draft"][0]
                    self.assertTrue(cache["deleted"])
                    self.assertEqual(cache["content"], "")
                    self.assertGreater(event["draft_revision"], prior_revision)
        self.assertEqual(text_count, 2)
        self.assertEqual(emitted[-1]["status"], "completed")
        self.assertEqual(self._drafts(), [])

    def test_disconnect_after_text_recovers_every_received_character_without_chapter_or_facts(self):
        self._nodes(2)
        before = self._rows()
        stream = auto_pilot.run_autopilot(self.novel_id, session_factory=self.sessions)
        with patch.object(auto_pilot.llm, "stream_complete", return_value=iter(["已经收到。", "末尾中文半句话", "未收到"] )):
            received = []
            while len(received) < 2:
                event = _event(next(stream))
                if event["type"] == "text":
                    received.append(event["content"])
            stream.close()
        self.assertFalse(auto_pilot.registry.is_running(self.novel_id))
        recovered = self._drafts()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["content"], "".join(received))
        self.assertEqual(recovered[0]["state"], "unconfirmed")
        self.assertIn("尚未确认", recovered[0]["reason"])
        after = self._rows()
        for name in before:
            if name != "autopilot_draft":
                self.assertEqual(after[name], before[name], name)

    def test_failure_threshold_preserves_separate_complete_failed_drafts_and_no_chapters(self):
        nodes = self._nodes(4)
        with patch.object(auto_pilot.llm, "stream_complete", side_effect=lambda *a, **kw: _truncated()):
            emitted = self._run()
        self.assertEqual((emitted[-1]["status"], emitted[-1]["reason"]), ("incomplete", "failure_threshold"))
        drafts = self._drafts()
        self.assertEqual(len(drafts), 3)
        self.assertEqual({row["node_id"] for row in drafts}, {node.id for node in nodes[:3]})
        self.assertTrue(all(row["state"] == "failed" for row in drafts))
        self.assertTrue(all(row["content"] == "失败时收到的第一段。末尾仍有半句话" for row in drafts))
        self.assertTrue(all(row["attempt"] == 1 for row in drafts))
        errors = [event for event in emitted if event["type"] == "error"]
        self.assertEqual(len(errors), 3)
        self.assertTrue(all(event["draft_persisted"] and not event["saved"] for event in errors))
        self.assertEqual({event["draft_id"] for event in errors}, {row["id"] for row in drafts})
        self.assertEqual(self._rows()["chapter"], [])

    def test_retry_keeps_failed_history_and_clears_only_new_successful_attempt(self):
        self._nodes(1)
        with patch.object(auto_pilot.llm, "stream_complete", side_effect=lambda *a, **kw: _truncated()):
            self._run()
        historical = self._rows()["autopilot_draft"][0]
        with patch.object(auto_pilot.llm, "stream_complete", return_value=iter(["重试后完整正文。"] )):
            emitted = self._run()
        after = self._rows()
        old = next(row for row in after["autopilot_draft"] if row["id"] == historical["id"])
        self.assertEqual(old, historical)
        new = next(row for row in after["autopilot_draft"] if row["id"] != historical["id"])
        self.assertEqual(new["attempt"], 2)
        self.assertTrue(new["deleted"])
        self.assertEqual(new["content"], "")
        self.assertEqual(self._drafts()[0]["id"], historical["id"])
        self.assertEqual(after["chapter"][0]["content"], "重试后完整正文。")
        self.assertEqual(emitted[-1]["status"], "completed")
        self.assertEqual(emitted[-1]["failed"], 0)

    def test_cleanup_failure_after_success_does_not_fail_body_and_is_retried_at_task_end(self):
        self._nodes(2)
        self._sql(
            "CREATE TRIGGER delayed_first_cleanup BEFORE UPDATE ON autopilot_draft "
            "WHEN NEW.deleted = 1 AND (SELECT count(*) FROM chapter) < 2 "
            "BEGIN SELECT RAISE(ABORT, 'cleanup unavailable until next chapter'); END"
        )
        with patch.object(auto_pilot.llm, "stream_complete", side_effect=lambda *a, **kw: iter(["各章完整正文。"])):
            emitted = self._run()
        self.assertEqual(len(self._rows()["chapter"]), 2)
        self.assertEqual(sum(event["type"] == "chapter_done" for event in emitted), 2)
        errors = [event for event in emitted if event["type"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual((errors[0]["scope"], errors[0]["saved"], errors[0]["code"]),
                         ("quality", True, "draft_cleanup_failed"))
        self.assertEqual((emitted[-1]["status"], emitted[-1]["failed"]), ("completed", 0))
        self.assertEqual(self._drafts(), [])
        self.assertTrue(all(row["deleted"] and not row["content"] for row in self._rows()["autopilot_draft"]))

    def test_cache_write_failure_never_sends_unsaved_text_or_claims_all_output_recoverable(self):
        self._nodes(1)
        self._sql(
            "CREATE TRIGGER reject_unsaved_text BEFORE UPDATE ON autopilot_draft "
            "WHEN NEW.content LIKE '%这段不能持久化%' "
            "BEGIN SELECT RAISE(ABORT, 'draft disk failure'); END"
        )
        with patch.object(auto_pilot.llm, "stream_complete", return_value=iter(["已确认持久化。", "这段不能持久化"] )):
            emitted = self._run()
        delivered = [event for event in emitted if event["type"] == "text"]
        self.assertEqual([event["content"] for event in delivered], ["已确认持久化。"])
        self.assertTrue(delivered[0]["draft_persisted"])
        failure = next(event for event in emitted if event.get("scope") == "chapter")
        self.assertFalse(failure["draft_persisted"])
        self.assertIn("请先复制", failure["message"])
        self.assertFalse(any(event["type"] == "chapter_done" for event in emitted))
        self.assertEqual(self._rows()["chapter"], [])
        self.assertEqual(self._drafts()[0]["content"], "已确认持久化。")
        self.assertEqual(emitted[-1]["status"], "incomplete")
        self.assertFalse(auto_pilot.registry.is_running(self.novel_id))

    def test_persistent_cleanup_failure_reports_cache_problem_without_claiming_missing_chapter(self):
        self._nodes(1)
        self._sql(
            "CREATE TRIGGER reject_all_cleanup BEFORE UPDATE ON autopilot_draft "
            "WHEN NEW.deleted = 1 BEGIN SELECT RAISE(ABORT, 'cleanup stays unavailable'); END"
        )
        with patch.object(auto_pilot.llm, "stream_complete", return_value=iter(["正文确实已经保存。"] )):
            emitted = self._run()
        self.assertEqual(len(self._rows()["chapter"]), 1)
        self.assertEqual(sum(event["type"] == "chapter_done" for event in emitted), 1)
        terminal = emitted[-1]
        self.assertEqual((terminal["generated"], terminal["failed"], terminal["done"], terminal["total"]),
                         (1, 0, 1, 1))
        self.assertNotIn("未完成章节", terminal["message"])
        self.assertIn("正文", terminal["message"])
        self.assertIn("缓存", terminal["message"])
        self.assertFalse(auto_pilot.registry.is_running(self.novel_id))

    def test_start_cache_failure_does_not_call_model_or_send_fake_persisted_progress(self):
        self._nodes(1)
        self._sql(
            "CREATE TRIGGER reject_new_cache BEFORE INSERT ON autopilot_draft "
            "BEGIN SELECT RAISE(ABORT, 'draft storage unavailable'); END"
        )
        with patch.object(auto_pilot.llm, "stream_complete") as llm_stream:
            emitted = self._run()
        llm_stream.assert_not_called()
        self.assertFalse(any(event["type"] == "text" for event in emitted))
        self.assertFalse(any(event.get("phase") == "generating" for event in emitted))
        failure = next(event for event in emitted if event.get("scope") == "chapter")
        self.assertFalse(failure["draft_persisted"])
        self.assertEqual(self._rows()["autopilot_draft"], [])
        self.assertEqual(self._rows()["chapter"], [])

    def test_failed_chapter_insert_rolls_back_sort_shift_and_keeps_recoverable_body(self):
        nodes = self._nodes(3)
        first = repos.ChapterRepo(self.db).create(
            self.novel_id, outline_node_id=nodes[0].id, seq=1, title="第一章", content="原首章",
        )
        third = repos.ChapterRepo(self.db).create(
            self.novel_id, outline_node_id=nodes[2].id, seq=2, title="第三章", content="原第三章",
        )
        before = self._rows()["chapter"]
        self._sql(
            "CREATE TRIGGER reject_new_chapter BEFORE INSERT ON chapter "
            "BEGIN SELECT RAISE(ABORT, 'chapter insert failure'); END"
        )
        with patch.object(auto_pilot.llm, "stream_complete", return_value=iter(["缺失中章已生成的正文。"] )):
            emitted = self._run()
        self.assertEqual(self._rows()["chapter"], before)
        self.assertEqual({row["id"] for row in before}, {first.id, third.id})
        drafts = self._drafts()
        self.assertEqual(len(drafts), 1)
        self.assertEqual((drafts[0]["node_id"], drafts[0]["state"]), (nodes[1].id, "failed"))
        self.assertEqual(drafts[0]["content"], "缺失中章已生成的正文。")
        self.assertFalse(any(event["type"] == "chapter_done" for event in emitted))
        self.assertEqual((emitted[-1]["generated"], emitted[-1]["skipped"], emitted[-1]["failed"]), (0, 2, 1))


if __name__ == "__main__":
    unittest.main()
