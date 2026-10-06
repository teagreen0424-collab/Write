"""显式篇幅范围：真实保存、确定性暂停、快照重连和幂等身份。"""
import json
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.application import auto_pilot, autopilot_tasks
from app.application.word_count import check_word_limits, count_words, normalize_word_limits
from app.infrastructure import models, repository as repos
from app.infrastructure.autopilot_tasks import AutopilotTaskRepo, TaskStorageError
from app.interfaces.schemas import AutopilotTaskSnapshot, StoryCheckResult


def event(frame):
    pieces = [line[5:].strip() for line in frame.splitlines() if line.startswith("data:")]
    return json.loads("\n".join(pieces)) if pieces else None


class WordCountTest(unittest.TestCase):
    def test_unicode_whitespace_punctuation_and_non_bmp_characters(self):
        self.assertEqual(count_words(" \t林\n舟，😀\r\n\u3000\u00a0\u0085\u001c"), 4)
        self.assertEqual(count_words("A。\u200b"), 3)
        self.assertEqual(count_words(""), 0)

    def test_closed_bounds_and_single_sided_ranges(self):
        for size in (3, 5):
            self.assertTrue(check_word_limits("字" * size, {"min_words": 3, "max_words": 5})["within_limits"])
        self.assertFalse(check_word_limits("字字", {"min_words": 3})["within_limits"])
        self.assertFalse(check_word_limits("字" * 6, {"max_words": 5})["within_limits"])
        self.assertTrue(check_word_limits("字" * 100, {"min_words": 3})["within_limits"])
        self.assertTrue(check_word_limits("字", {"max_words": 5})["within_limits"])

    def test_missing_range_is_count_only_without_invented_bounds(self):
        result = check_word_limits("短稿。", None)
        self.assertEqual(result, {"word_count": 3, "min_words": None, "max_words": None,
                                  "within_limits": None, "warnings": []})

    def test_readonly_editor_check_does_not_claim_body_was_saved(self):
        unsaved = check_word_limits("未保存正文。", {"max_words": 2}, "编辑中的章")
        saved = check_word_limits("已保存正文。", {"max_words": 2}, "自动生成章", saved=True)
        self.assertNotIn("正文已保存", unsaved["warnings"][0])
        self.assertIn("正文已保存", saved["warnings"][0])
        self.assertFalse(unsaved["within_limits"])

    def test_internal_limits_reject_coercions_and_invalid_ranges(self):
        for value in ({}, {"min_words": None}, {"min_words": True}, {"max_words": 1.5},
                      {"min_words": "3"}, {"min_words": 0}, {"max_words": 1_000_001},
                      {"min_words": 4, "max_words": 3}, {"max_words": 5, "extra": 1}, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_word_limits(value)
        self.assertEqual(normalize_word_limits({"max_words": 1_000_000}),
                         {"min_words": None, "max_words": 1_000_000})


class _SqliteTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = (Path(directory.name) / "word-count.db").as_posix()
        self.engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
        models.Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.sessions()
        self.addCleanup(self.db.close)
        self.novel_id = repos.NovelRepo(self.db).create(title="作者篇幅设置验收").id
        self.registry = auto_pilot.AutoPilotRegistry()
        for mocked in (
            patch.object(auto_pilot, "registry", self.registry),
            patch.object(auto_pilot, "build_context", return_value="Story Bible"),
            patch.object(auto_pilot, "review_story", return_value=StoryCheckResult()),
            patch.object(auto_pilot, "consistency_check", return_value={"conflicts": []}),
            patch.object(auto_pilot, "score_tension", return_value=3),
            patch.object(auto_pilot, "check_drift", return_value=None),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)

    def nodes(self, count):
        return [repos.OutlineRepo(self.db).create(
            self.novel_id, level="chapter", title=f"第{index}章", sort_order=index,
        ) for index in range(1, count + 1)]

    def collect(self, stream):
        return [decoded for frame in stream if (decoded := event(frame)) is not None]

    def to_pause(self, stream):
        emitted = []
        for _ in range(30):
            decoded = event(next(stream))
            if decoded is not None:
                emitted.append(decoded)
                if decoded["type"] == "paused":
                    return emitted
        self.fail("越界后没有在下一未生成章前暂停")

    def rows(self):
        with self.sessions() as observer:
            return {table.name: [dict(row) for row in observer.execute(select(table)).mappings()]
                    for table in models.Base.metadata.sorted_tables}


class AutopilotWordCountTest(_SqliteTest):
    def test_over_limit_saves_complete_body_and_pauses_before_next_generation(self):
        self.nodes(2)
        body = "完整正文，😀 不截断。"
        with patch.object(auto_pilot.llm, "stream_complete", side_effect=lambda *a, **kw: iter([body])) as write:
            stream = auto_pilot.run_autopilot(self.novel_id, self.sessions, word_limits={"max_words": 5})
            self.addCleanup(stream.close)
            emitted = self.to_pause(stream)
            write.assert_called_once()
            word = next(item for item in emitted if item["type"] == "word_check")
            self.assertFalse(word["within_limits"])
            self.assertEqual(word["word_count"], count_words(body))
            self.assertEqual((word["min_words"], word["max_words"], word["saved"]), (None, 5, True))
            self.assertIn("超过", word["warnings"][0])
            saved = repos.ChapterRepo(self.db).list(self.novel_id)
            self.assertEqual([(c.content, c.status, c.summary) for c in saved], [(body, "draft", "")])
            done = next(item for item in emitted if item["type"] == "chapter_done")
            self.assertEqual((done["generated"], done["failed"]), (1, 0))
            self.assertLess(emitted.index(done), emitted.index(word))
            self.assertIn("至多 5 字", write.call_args.args[0][1]["content"])
            self.registry.get(self.novel_id).stop = True
            terminal = self.collect(stream)[-1]
            self.assertEqual((terminal["status"], terminal["generated"], terminal["failed"]), ("stopped", 1, 0))

    def test_under_limit_also_pauses_without_failed_draft(self):
        self.nodes(2)
        with patch.object(auto_pilot.llm, "stream_complete", return_value=iter(["短稿。"])):
            stream = auto_pilot.run_autopilot(self.novel_id, self.sessions, word_limits={"min_words": 4})
            self.addCleanup(stream.close)
            emitted = self.to_pause(stream)
            word = next(item for item in emitted if item["type"] == "word_check")
            self.assertIn("少于", word["warnings"][0])
            self.assertEqual(len(repos.ChapterRepo(self.db).list(self.novel_id)), 1)
            self.registry.get(self.novel_id).stop = True
            self.assertEqual(self.collect(stream)[-1]["failed"], 0)

    def test_exact_boundaries_continue_and_do_not_inherit_quality_failure(self):
        self.nodes(2)
        with patch.object(auto_pilot.llm, "stream_complete", side_effect=[iter(["三字。"]), iter(["五个字，。"])]) as write:
            emitted = self.collect(auto_pilot.run_autopilot(
                self.novel_id, self.sessions, word_limits={"min_words": 3, "max_words": 5},
            ))
        words = [item for item in emitted if item["type"] == "word_check"]
        self.assertEqual([item["word_count"] for item in words], [3, 5])
        self.assertTrue(all(item["within_limits"] and not item["warnings"] for item in words))
        self.assertNotIn("paused", [item["type"] for item in emitted])
        self.assertEqual((emitted[-1]["status"], emitted[-1]["generated"], emitted[-1]["failed"]), ("completed", 2, 0))
        self.assertIn("3–5 字", write.call_args.args[0][1]["content"])

    def test_last_chapter_outside_range_completes_with_persisted_warning(self):
        self.nodes(1)
        task_id = str(uuid.uuid4())
        limits = {"min_words": 4, "max_words": None}
        task_repo = AutopilotTaskRepo(self.db)
        task_repo.create(self.novel_id, task_id, limits)
        with patch.object(auto_pilot.llm, "stream_complete", return_value=iter(["短稿。"])):
            emitted = self.collect(auto_pilot.run_autopilot(self.novel_id, self.sessions, word_limits=limits))
        for item in emitted:
            task_repo.append(self.novel_id, task_id, item)
        with self.sessions() as reconnect:
            snapshot = AutopilotTaskRepo(reconnect).get(self.novel_id, task_id)
        self.assertEqual((snapshot["status"], snapshot["generated"], snapshot["failed"]), ("completed", 1, 0))
        self.assertEqual(snapshot["word_limits"], limits)
        self.assertEqual(len(snapshot["quality_warnings"]), 1)
        self.assertIn("少于", snapshot["quality_warnings"][0])
        self.assertIn("警告", snapshot["message"])
        self.assertNotIn("paused", [item["type"] for item in emitted])

    def test_word_result_survives_model_review_failure_and_refresh(self):
        self.nodes(2)
        task_id = str(uuid.uuid4())
        task_repo = AutopilotTaskRepo(self.db)
        task_repo.create(self.novel_id, task_id, {"max_words": 2})
        with (patch.object(auto_pilot.llm, "stream_complete", return_value=iter(["完整保留稿。"])),
              patch.object(auto_pilot, "review_story", side_effect=RuntimeError("模型暂时不可用"))):
            stream = auto_pilot.run_autopilot(self.novel_id, self.sessions, word_limits={"max_words": 2})
            self.addCleanup(stream.close)
            emitted = self.to_pause(stream)
            for item in emitted:
                task_repo.append(self.novel_id, task_id, item)
            with self.sessions() as reconnect:
                snapshot = AutopilotTaskRepo(reconnect).get(self.novel_id, task_id)
                _, replay = AutopilotTaskRepo(reconnect).read_batch(self.novel_id, task_id, 0)
            self.assertEqual((snapshot["status"], snapshot["generated"], snapshot["failed"]), ("paused", 1, 0))
            self.assertEqual(len(snapshot["quality_warnings"]), 2)
            self.assertTrue(any("超过" in warning for warning in snapshot["quality_warnings"]))
            self.assertTrue(any("复核未能完成" in warning for warning in snapshot["quality_warnings"]))
            self.assertEqual(next(item for item in replay if item["type"] == "word_check")["word_count"], 6)
            self.assertLess([item["type"] for item in emitted].index("word_check"),
                            [item["type"] for item in emitted].index("error"))
            self.registry.get(self.novel_id).stop = True
            self.collect(stream)

    def test_absent_range_does_not_parse_directive_or_add_gate(self):
        self.nodes(2)
        state = self.registry.start(self.novel_id)
        state.injected_directive = "正文1200到1600字"
        with patch.object(auto_pilot.llm, "stream_complete", side_effect=lambda *a, **kw: iter(["短稿。"])):
            emitted = self.collect(auto_pilot.run_autopilot(self.novel_id, self.sessions, reserved_state=state))
        self.assertNotIn("word_check", [item["type"] for item in emitted])
        self.assertNotIn("paused", [item["type"] for item in emitted])
        self.assertEqual((emitted[-1]["generated"], emitted[-1]["failed"]), (2, 0))

    def test_story_event_retains_coverage_metadata_and_empty_failure_shape(self):
        result = StoryCheckResult(
            requirements=[{"id": "directive-1", "source": "directive", "text": "明确拒绝偷档案"}],
            requirement_checks=[{"requirement_id": "directive-1", "status": "uncertain", "excerpt": "",
                                 "reason": "当前证据不足"}],
            unreviewed_requirement_ids=["directive-1"], review_complete=False,
        )
        for mode in ("structured", "failure"):
            with self.subTest(mode=mode):
                self.novel_id = repos.NovelRepo(self.db).create(title=f"核验覆盖元数据{mode}").id
                self.nodes(1)
                review_kwargs = {"return_value": result} if mode == "structured" else {"side_effect": RuntimeError("审校失败")}
                with (patch.object(auto_pilot.llm, "stream_complete", return_value=iter(["完整正文"])),
                      patch.object(auto_pilot, "review_story", **review_kwargs)):
                    emitted = self.collect(auto_pilot.run_autopilot(self.novel_id, self.sessions))
                check = next(item for item in emitted if item["type"] == "story_check")
                self.assertFalse(check["review_complete"])
                expected = result.model_dump() if mode == "structured" else {}
                for key in ("requirements", "requirement_checks", "unreviewed_requirement_ids"):
                    self.assertEqual(check[key], expected.get(key, []))

    def test_manager_keeps_author_range_on_pause_resume_and_reconnect(self):
        self.nodes(2)
        manager = autopilot_tasks.AutopilotTaskManager(
            session_factory=self.sessions, registry=self.registry,
            heartbeat_seconds=0.02, shutdown_timeout=2,
        )
        self.addCleanup(manager.shutdown)
        task_id = str(uuid.uuid4())
        limits = {"min_words": None, "max_words": 3}
        with patch.object(auto_pilot.llm, "stream_complete", side_effect=[iter(["超长的第一稿。"]), iter(["短稿。"])]) as write:
            manager.start(self.novel_id, task_id, limits)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and manager.get(self.novel_id, task_id)["status"] != "paused":
                threading.Event().wait(0.01)
            paused = manager.get(self.novel_id, task_id)
            self.assertEqual((paused["status"], paused["generated"], paused["failed"]), ("paused", 1, 0))
            self.assertEqual(paused["word_limits"], limits)
            self.assertEqual(self.registry.get(self.novel_id).word_limits, limits)
            self.assertEqual(event(next(manager.subscribe(self.novel_id, task_id)))["word_limits"], limits)
            manager.resume(self.novel_id, task_id)
            self.assertTrue(manager.wait(task_id, timeout=5))
            terminal = manager.get(self.novel_id, task_id)
            self.assertEqual((terminal["status"], terminal["generated"], terminal["failed"]), ("completed", 2, 0))
            self.assertEqual(terminal["word_limits"], limits)
            self.assertEqual(len(terminal["quality_warnings"]), 1)
            self.assertEqual(write.call_count, 2)


class WordCountTaskStoreTest(_SqliteTest):
    def test_request_id_limits_are_immutable_and_conflict_after_completion(self):
        task_id = str(uuid.uuid4())
        repo = AutopilotTaskRepo(self.db)
        limits = {"max_words": 50}
        initial, created = repo.create(self.novel_id, task_id, limits)
        self.assertTrue(created)
        limits["max_words"] = 100
        expected = {"min_words": None, "max_words": 50}
        self.assertEqual(initial["word_limits"], expected)
        self.assertFalse(repo.create(self.novel_id, task_id, expected)[1])
        repo.append(self.novel_id, task_id, {"type": "done", "status": "completed"})
        before = self.rows()
        for requested in (limits, None, {"min_words": 3, "max_words": 50}):
            with self.subTest(requested=requested), self.assertRaises(TaskStorageError) as caught:
                repo.create(self.novel_id, task_id, requested)
            self.assertEqual((caught.exception.status_code, caught.exception.code, caught.exception.task_id),
                             (409, "autopilot_request_conflict", task_id))
        self.assertEqual(self.rows(), before)
        self.assertEqual(repo.create(self.novel_id, task_id, expected)[0]["word_limits"], expected)

    def test_word_warning_projection_is_deduplicated_and_replay_keeps_limit(self):
        task_id = str(uuid.uuid4())
        repo = AutopilotTaskRepo(self.db)
        repo.create(self.novel_id, task_id, {"min_words": 5})
        check = {"type": "word_check", "word_count": 3, "min_words": 5, "max_words": None,
                 "within_limits": False, "warnings": ["完整正文保留，少于5字。"]}
        repo.append(self.novel_id, task_id, check)
        repo.append(self.novel_id, task_id, check)
        with self.sessions() as reconnect:
            snapshot, events = AutopilotTaskRepo(reconnect).read_batch(self.novel_id, task_id, 0)
        self.assertEqual(snapshot["quality_warnings"], check["warnings"])
        self.assertEqual(snapshot["word_limits"], {"min_words": 5, "max_words": None})
        self.assertEqual([item["word_count"] for item in events], [3, 3])
        AutopilotTaskSnapshot.model_validate(snapshot)

    def test_old_snapshot_without_limits_remains_idempotent_and_valid(self):
        task_id = str(uuid.uuid4())
        repo = AutopilotTaskRepo(self.db)
        initial, _ = repo.create(self.novel_id, task_id)
        initial.pop("word_limits")
        stored = self.db.get(models.AutopilotTask, task_id)
        stored.snapshot_json = initial
        self.db.commit()
        same, created = repo.create(self.novel_id, task_id)
        self.assertFalse(created)
        self.assertNotIn("word_limits", same)
        self.assertIsNone(AutopilotTaskSnapshot.model_validate(same).word_limits)


if __name__ == "__main__":
    unittest.main()
