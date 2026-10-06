"""自动驾驶多章失败、重试、质量错误与明确终态的内存 SQLite 验收。"""
import json
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.application import auto_pilot
from app.infrastructure import models
from app.infrastructure.ai.llm_provider import LLMStreamIncompleteError
from app.infrastructure import repository as repos
from app.interfaces.schemas import StoryCheckResult


def _event(frame):
    return json.loads(frame[5:].strip())


def _truncated():
    yield "失败章仍可见的半稿。"
    yield "最后半句话"
    raise LLMStreamIncompleteError("输出上限，生成未完成。", code="output_truncated", finish_reason="length")


class AutoPilotEventTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        models.Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.session_factory()
        self.novel_id = repos.NovelRepo(self.db).create(title="多章边界测试").id
        self.patches = [
            patch.object(auto_pilot, "registry", auto_pilot.AutoPilotRegistry()),
            patch.object(auto_pilot, "build_context", return_value="测试设定"),
            patch.object(auto_pilot, "review_story", return_value=StoryCheckResult()),
            patch.object(auto_pilot, "score_tension", return_value=6.0),
            patch.object(auto_pilot, "check_drift", return_value=None),
        ]
        for mocked in self.patches:
            mocked.start()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        for mocked in self.patches:
            self.addCleanup(mocked.stop)

    def _nodes(self, count):
        return [
            repos.OutlineRepo(self.db).create(
                self.novel_id, title=f"第{index}章", level="chapter", sort_order=index,
                summary=f"第{index}章目标",
            ) for index in range(1, count + 1)
        ]

    def _run(self, **kwargs):
        return [_event(frame) for frame in auto_pilot.run_autopilot(
            self.novel_id, session_factory=kwargs.get("session_factory", self.session_factory)
        )]

    def _rows(self):
        self.db.expire_all()
        return {
            table.name: sorted(
                [dict(row) for row in self.db.execute(select(table)).mappings()],
                key=lambda row: row["id"],
            )
            for table in models.Base.metadata.sorted_tables
            if table.name != "autopilot_draft"  # 恢复缓存单独验证，不属于正式章节/作品资料。
        }

    def _terminal(self, emitted):
        self.assertEqual(emitted[-1]["type"], "done")
        self.assertEqual(sum(event["type"] == "done" for event in emitted), 1)
        self.assertFalse(auto_pilot.registry.is_running(self.novel_id))
        return emitted[-1]

    def test_success_and_truncation_keep_half_draft_visible_without_persisting_it(self):
        nodes = self._nodes(3)
        outputs = (iter(["第一章完整正文。"]), _truncated(), iter(["第三章完整正文。"]))
        with patch.object(auto_pilot.llm, "stream_complete", side_effect=outputs):
            emitted = self._run()

        chapters = repos.ChapterRepo(self.db).list(self.novel_id)
        self.assertEqual([chapter.outline_node_id for chapter in chapters], [nodes[0].id, nodes[2].id])
        self.assertEqual([chapter.content for chapter in chapters], ["第一章完整正文。", "第三章完整正文。"])
        self.assertEqual(self._rows()["chapter_revision"], [])
        received = "".join(
            event["content"] for event in emitted
            if event["type"] == "text" and event["node_id"] == nodes[1].id
        )
        self.assertEqual(received, "失败章仍可见的半稿。最后半句话")
        error = next(event for event in emitted if event["type"] == "error")
        self.assertEqual(error["scope"], "chapter")
        self.assertEqual(error["code"], "output_truncated")
        self.assertFalse(error["saved"])
        self.assertFalse(error["fatal"])
        self.assertEqual(error["title"], nodes[1].title)
        self.assertIn("未保存", error["message"])
        self.assertFalse(any(
            event["type"] == "chapter_done" and event["node_id"] == nodes[1].id for event in emitted
        ))
        terminal = self._terminal(emitted)
        self.assertEqual((terminal["status"], terminal["generated"], terminal["failed"]), ("incomplete", 2, 1))
        self.assertEqual((terminal["done"], terminal["total"]), (2, 3))

    def test_three_consecutive_failures_end_without_fake_pause_or_writes(self):
        self._nodes(4)
        before = self._rows()
        with patch.object(auto_pilot.llm, "stream_complete", side_effect=lambda *a, **kw: _truncated()) as stream:
            emitted = self._run()
        self.assertEqual(stream.call_count, 3)
        self.assertEqual(self._rows(), before)
        self.assertNotIn("paused", [event["type"] for event in emitted])
        terminal = self._terminal(emitted)
        self.assertEqual((terminal["status"], terminal["reason"]), ("incomplete", "failure_threshold"))
        self.assertEqual((terminal["generated"], terminal["skipped"], terminal["failed"]), (0, 0, 3))
        self.assertEqual((terminal["done"], terminal["total"]), (0, 4))

    def test_retry_only_generates_missing_node_and_preserves_successful_rows(self):
        nodes = self._nodes(3)
        with patch.object(auto_pilot.llm, "stream_complete", side_effect=(
            iter(["第一章原稿。"]), _truncated(), iter(["第三章原稿。"]),
        )):
            self._run()
        before = self._rows()
        with patch.object(auto_pilot.llm, "stream_complete", return_value=iter(["第二章补完。"])) as stream:
            emitted = self._run()
        stream.assert_called_once()
        self.assertIn("第2章目标", stream.call_args.args[0][1]["content"])
        skipped = [event for event in emitted if event.get("phase") == "skipped"]
        self.assertEqual([event["node_id"] for event in skipped], [nodes[0].id, nodes[2].id])
        after = self._rows()
        old_ids = {row["id"] for row in before["chapter"]}
        expected_old = [
            {**row, "seq": row["seq"] + (row["outline_node_id"] == nodes[2].id)}
            for row in before["chapter"]
        ]
        self.assertEqual([row for row in after["chapter"] if row["id"] in old_ids], expected_old)
        for table in before:
            if table != "chapter":
                self.assertEqual(after[table], before[table])
        chapters = repos.ChapterRepo(self.db).list(self.novel_id)
        self.assertEqual(len(chapters), 3)
        self.assertEqual(len({chapter.outline_node_id for chapter in chapters}), 3)
        self.assertEqual([chapter.outline_node_id for chapter in chapters], [node.id for node in nodes])
        terminal = self._terminal(emitted)
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual((terminal["generated"], terminal["skipped"], terminal["failed"]), (1, 2, 0))
        self.assertEqual((terminal["done"], terminal["total"]), (3, 3))

    def test_all_existing_nodes_finish_at_full_progress_without_calling_model(self):
        for node in self._nodes(2):
            repos.ChapterRepo(self.db).create(
                self.novel_id, title=node.title, content="完整旧稿。", outline_node_id=node.id,
            )
        before = self._rows()
        with patch.object(auto_pilot.llm, "stream_complete") as stream:
            emitted = self._run()
        stream.assert_not_called()
        self.assertEqual(self._rows(), before)
        self.assertEqual([event["phase"] for event in emitted if event["type"] == "progress"],
                         ["starting", "skipped", "skipped"])
        terminal = self._terminal(emitted)
        self.assertEqual((terminal["status"], terminal["done"], terminal["total"]), ("completed", 2, 2))
        self.assertEqual((terminal["generated"], terminal["skipped"], terminal["failed"]), (0, 2, 0))

    def test_quality_failures_keep_saved_chapters_and_do_not_trip_failure_threshold(self):
        self._nodes(3)
        with (
            patch.object(auto_pilot.llm, "stream_complete", side_effect=lambda *a, **kw: iter(["完整正文。"])),
            patch.object(auto_pilot, "score_tension", side_effect=RuntimeError("评分不可用")),
            patch.object(auto_pilot, "check_drift", side_effect=RuntimeError("文风检测不可用")),
        ):
            emitted = self._run()
        self.assertEqual(len(repos.ChapterRepo(self.db).list(self.novel_id)), 3)
        errors = [event for event in emitted if event["type"] == "error"]
        self.assertEqual(len(errors), 6)
        self.assertTrue(all(event["scope"] == "quality" and event["saved"] for event in errors))
        self.assertEqual({event["kind"] for event in errors}, {"tension", "drift"})
        self.assertTrue(all("正文已保存" in event["message"] for event in errors))
        terminal = self._terminal(emitted)
        self.assertEqual((terminal["status"], terminal["generated"], terminal["failed"]), ("completed", 3, 0))

    def test_success_between_failures_resets_consecutive_failure_counter(self):
        self._nodes(4)
        with patch.object(auto_pilot.llm, "stream_complete", side_effect=(
            _truncated(), _truncated(), iter(["第三章成功。"]), _truncated(),
        )) as stream:
            emitted = self._run()
        self.assertEqual(stream.call_count, 4)
        terminal = self._terminal(emitted)
        self.assertEqual((terminal["failed"], terminal["generated"]), (3, 1))
        self.assertNotEqual(terminal["reason"], "failure_threshold")
        self.assertEqual(len(repos.ChapterRepo(self.db).list(self.novel_id)), 1)

    def test_busy_is_terminal_task_error_and_keeps_existing_owner(self):
        existing = auto_pilot.registry.start(self.novel_id)
        emitted = self._run()
        self.assertEqual(len(emitted), 1)
        self.assertEqual((emitted[0]["scope"], emitted[0]["fatal"], emitted[0]["code"]),
                         ("task", True, "autopilot_busy"))
        self.assertIs(auto_pilot.registry.get(self.novel_id), existing)
        auto_pilot.registry.finish(self.novel_id)

    def test_session_creation_failure_releases_registry_and_has_terminal_done(self):
        with patch.object(auto_pilot, "_ordered_chapter_nodes") as order:
            stream = auto_pilot.run_autopilot(
                self.novel_id,
                session_factory=lambda: (_ for _ in ()).throw(RuntimeError("会话启动失败")),
            )
            first = _event(next(stream))
            self.assertFalse(auto_pilot.registry.is_running(self.novel_id))
            emitted = [first, *(_event(frame) for frame in stream)]
        order.assert_not_called()
        self.assertEqual((emitted[0]["type"], emitted[0]["scope"], emitted[0]["fatal"]),
                         ("error", "task", True))
        self.assertEqual(self._terminal(emitted)["status"], "incomplete")

    def test_outline_failure_has_terminal_done_and_releases_registry(self):
        with patch.object(auto_pilot, "_ordered_chapter_nodes", side_effect=RuntimeError("大纲不可用")):
            stream = auto_pilot.run_autopilot(self.novel_id, session_factory=self.session_factory)
            first = _event(next(stream))
            self.assertFalse(auto_pilot.registry.is_running(self.novel_id))
            emitted = [first, *(_event(frame) for frame in stream)]
        self.assertEqual((emitted[0]["scope"], emitted[0]["fatal"]), ("task", True))
        self.assertEqual(self._terminal(emitted)["status"], "incomplete")

    def test_manual_pause_keeps_stream_alive_until_resume_and_stop_is_explicit(self):
        self._nodes(1)
        stream = auto_pilot.run_autopilot(self.novel_id, session_factory=self.session_factory)
        self.assertEqual(_event(next(stream))["phase"], "starting")
        auto_pilot.registry.pause(self.novel_id)
        self.assertEqual(_event(next(stream))["type"], "paused")
        self.assertTrue(auto_pilot.registry.is_running(self.novel_id))
        with (
            patch.object(auto_pilot.time, "sleep", side_effect=lambda _: auto_pilot.registry.resume(self.novel_id)),
            patch.object(auto_pilot.llm, "stream_complete", return_value=iter(["完整正文。"])),
        ):
            emitted = [_event(frame) for frame in stream]
        self.assertEqual(self._terminal(emitted)["status"], "completed")

        stream = auto_pilot.run_autopilot(self.novel_id, session_factory=self.session_factory)
        next(stream)
        auto_pilot.registry.get(self.novel_id).stop = True
        emitted = [_event(frame) for frame in stream]
        terminal = self._terminal(emitted)
        self.assertEqual((terminal["status"], terminal["generated"]), ("stopped", 0))

    def test_registry_is_released_before_terminal_done_is_delivered(self):
        stream = auto_pilot.run_autopilot(self.novel_id, session_factory=self.session_factory)
        self.assertEqual(_event(next(stream))["phase"], "starting")
        terminal = _event(next(stream))
        self.assertEqual(terminal["status"], "completed")
        self.assertFalse(auto_pilot.registry.is_running(self.novel_id))
        stream.close()

    def test_closing_paused_stream_after_heartbeat_releases_registry(self):
        self._nodes(1)
        stream = auto_pilot.run_autopilot(self.novel_id, session_factory=self.session_factory)
        next(stream)
        auto_pilot.registry.pause(self.novel_id)
        self.assertEqual(_event(next(stream))["type"], "paused")
        with patch.object(auto_pilot.time, "sleep"):
            self.assertEqual(next(stream), ": paused\n\n")
        self.assertTrue(auto_pilot.registry.is_running(self.novel_id))
        stream.close()
        self.assertFalse(auto_pilot.registry.is_running(self.novel_id))

    def test_old_owner_cannot_finish_pause_or_inject_new_owner(self):
        first = auto_pilot.registry.start(self.novel_id, "first")
        auto_pilot.registry.finish(self.novel_id, "first")
        second = auto_pilot.registry.start(self.novel_id, "second")
        auto_pilot.registry.finish(self.novel_id, first.owner_id)
        self.assertIsNone(auto_pilot.registry.pause(self.novel_id, "first"))
        self.assertIsNone(auto_pilot.registry.resume(self.novel_id, "first"))
        self.assertIsNone(auto_pilot.registry.inject(self.novel_id, "旧构思", "first"))
        self.assertIs(auto_pilot.registry.get(self.novel_id), second)
        self.assertFalse(second.paused)
        self.assertEqual(second.injected_directive, "")
        auto_pilot.registry.finish(self.novel_id, "second")

    def test_reserved_state_runs_once_and_wrong_reservation_keeps_real_owner(self):
        state = auto_pilot.registry.start(self.novel_id, "task")
        wrong = auto_pilot.AutoPilotState("other")
        with patch.object(auto_pilot.llm, "stream_complete") as model:
            rejected = [_event(frame) for frame in auto_pilot.run_autopilot(
                self.novel_id, self.session_factory, reserved_state=wrong,
            )]
            self.assertEqual(rejected[0]["code"], "autopilot_task_ownership_lost")
            self.assertIs(auto_pilot.registry.get(self.novel_id), state)
            accepted = [_event(frame) for frame in auto_pilot.run_autopilot(
                self.novel_id, self.session_factory, reserved_state=state,
            )]
        model.assert_not_called()
        self.assertEqual(self._terminal(accepted)["status"], "completed")

    def test_shutdown_after_first_text_closes_model_without_saving_partial_chapter(self):
        self._nodes(1)
        closed = []
        advanced = []

        def output(*args, **kwargs):
            try:
                yield "停服前已发送。"
                advanced.append("不应请求下一段")
                yield "尚未发送的尾段"
            finally:
                closed.append(True)

        with patch.object(auto_pilot.llm, "stream_complete", side_effect=output):
            stream = auto_pilot.run_autopilot(self.novel_id, self.session_factory)
            emitted = []
            while not emitted or emitted[-1]["type"] != "text":
                emitted.append(_event(next(stream)))
            auto_pilot.registry.get(self.novel_id).stop = True
            emitted.extend(_event(frame) for frame in stream)
        self.assertEqual(self._terminal(emitted)["status"], "stopped")
        self.assertEqual(advanced, [])
        self.assertEqual(closed, [True])
        self.assertEqual(repos.ChapterRepo(self.db).list(self.novel_id), [])
        cached = self.db.query(models.AutopilotDraft).one()
        self.assertEqual((cached.content, cached.state, cached.deleted), ("停服前已发送。", "unconfirmed", False))

    def test_shutdown_during_consistency_check_does_not_commit_chapter(self):
        self._nodes(1)

        def stop_during_check(*args):
            auto_pilot.registry.get(self.novel_id).stop = True
            return {"conflicts": []}

        with (
            patch.object(auto_pilot.llm, "stream_complete", return_value=iter(["已暂存的完整片段。"])),
            patch.object(auto_pilot, "consistency_check", side_effect=stop_during_check),
        ):
            emitted = self._run()
        self.assertEqual(self._terminal(emitted)["status"], "stopped")
        self.assertEqual(repos.ChapterRepo(self.db).list(self.novel_id), [])
        cached = self.db.query(models.AutopilotDraft).one()
        self.assertEqual((cached.content, cached.state, cached.deleted), ("已暂存的完整片段。", "unconfirmed", False))


if __name__ == "__main__":
    unittest.main()
