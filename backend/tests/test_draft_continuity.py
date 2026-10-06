"""前章草稿承接只读隔离，以及生成后剧情质量暂停与持久警告。"""
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.application import auto_pilot, context, writing
from app.application.story_review import review_story as real_review_story
from app.infrastructure import models
from app.infrastructure import repository as repos
from app.infrastructure.autopilot_tasks import AutopilotTaskRepo
from app.interfaces.schemas import StoryCheckIssue, StoryCheckResult


def event(frame):
    return json.loads(frame[5:].strip()) if frame.startswith("data:") else None


class DraftContinuityTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = (Path(folder.name) / "draft-continuity.db").as_posix()
        self.engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
        models.Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.sessions()
        self.addCleanup(self.db.close)
        self.nid = repos.NovelRepo(self.db).create(title="前情验收").id
        self.other = repos.NovelRepo(self.db).create(title="不能带入的作品").id
        self.registry = auto_pilot.AutoPilotRegistry()
        for mocked in (
            patch.object(auto_pilot, "registry", self.registry),
            patch.object(auto_pilot, "score_tension", return_value=5.0),
            patch.object(auto_pilot, "check_drift", return_value=None),
            patch("app.application.context._semantic_recall", return_value=""),
            patch.object(auto_pilot, "review_story", return_value=StoryCheckResult()),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)

    def chapter(self, seq, content, **kwargs):
        return repos.ChapterRepo(self.db).create(self.nid, seq=seq, title=f"章节{seq}", content=content, **kwargs)

    def nodes(self, count):
        return [repos.OutlineRepo(self.db).create(
            self.nid, title=f"大纲{index}", level="chapter", sort_order=index,
            summary=f"推进目标{index}",
        ) for index in range(1, count + 1)]

    def rows(self):
        self.db.expire_all()
        return {table.name: [dict(row) for row in self.db.execute(select(table)).mappings()]
                for table in models.Base.metadata.sorted_tables}

    def collect(self, generator):
        return [decoded for frame in generator if (decoded := event(frame)) is not None]

    def run_to_pause(self, generator):
        emitted = []
        for _ in range(30):
            decoded = event(next(generator))
            if decoded is not None:
                emitted.append(decoded)
                if decoded["type"] == "paused":
                    return emitted
        self.fail("检测到问题后没有在下一章前暂停")

    def test_prior_draft_uses_head_and_tail_and_never_finalizes_or_summarizes(self):
        self.chapter(1, "最早旧章不应进入" )
        self.chapter(2, "早章正文", summary="两章前的摘要")
        self.chapter(3, "更早章首" + "中" * 1000 + "更早章尾")
        last = self.chapter(4, "身份首句" + "中" * 9000 + "约定明天下午")
        self.chapter(5, "当前章正文", status="finalized", summary="当前摘要不应进入")
        self.chapter(6, "未来正文", status="finalized", summary="未来摘要不应进入")
        repos.ChapterRepo(self.db).create(self.other, seq=1, title="跨作品", content="隐私跨作品正文")
        before = self.rows()
        prior = context.prior_chapter_context(self.db, self.nid, 5)
        self.assertEqual(prior.previous_title, last.title)
        self.assertTrue(prior.truncated)
        for text in ("身份首句", "约定明天下午", "两章前的摘要", "更早章首", "更早章尾", "不等于Story Bible定稿设定"):
            self.assertIn(text, prior.text)
        for text in ("最早旧章不应进入", "当前摘要不应进入", "未来摘要不应进入", "隐私跨作品正文"):
            self.assertNotIn(text, prior.text)
        self.assertEqual(len(context.chapter_excerpt(last.content)), 7000)
        self.assertEqual(self.rows(), before)

    def test_summary_and_recall_exclude_self_future_and_other_novel(self):
        first = self.chapter(1, "前章正文", status="finalized", summary="有效过去摘要")
        current = self.chapter(2, "当前正文", status="finalized", summary="不能带入自身摘要")
        future = self.chapter(3, "未来正文", status="finalized", summary="不能带入未来摘要")
        recalled = [
            {"text": "过去记忆", "source_chapter_id": first.id},
            {"text": "当前记忆", "source_chapter_id": current.id},
            {"text": "未来记忆", "source_chapter_id": future.id},
        ]
        # 解除setUp的空召回patch，直接验收有来源的召回过滤。
        with patch("app.infrastructure.vector.search", return_value=recalled):
            # unittest mock 保存原实现，避免进行任何嵌入调用。
            original_recall = self._original_recall
            with patch("app.application.context._semantic_recall", side_effect=original_recall):
                assembled = context.build_context(self.db, self.nid, "核对", chapter_seq=2)
        self.assertIn("有效过去摘要", assembled)
        self.assertIn("过去记忆", assembled)
        for text in ("不能带入自身摘要", "不能带入未来摘要", "当前记忆", "未来记忆"):
            self.assertNotIn(text, assembled)

    def test_outline_tree_predecessor_excludes_later_saved_chapter_when_filling_gap(self):
        nodes = self.nodes(3)
        earlier = self.chapter(1, "真正前章相识和下午约定", outline_node_id=nodes[0].id)
        self.chapter(2, "第三章后发生的未来内容", outline_node_id=nodes[2].id, status="finalized", summary="未来摘要")
        prior = context.prior_chapter_context(self.db, self.nid, outline_node_id=nodes[1].id)
        self.assertIn(earlier.content, prior.text)
        self.assertNotIn("未来", prior.text)
        self.assertEqual([c.id for c in context.prior_chapters(self.db, self.nid, outline_node_id=nodes[1].id)], [earlier.id])

    def test_implicit_manual_write_carries_latest_draft(self):
        previous = self.chapter(1, "上章已相识，约定次日下午见")
        with patch.object(writing.llm, "stream_complete", return_value=iter(["承接正文"])) as stream:
            emitted = self.collect(writing.write_chapter(self.db, self.nid))
        self.assertEqual(emitted[-1]["type"], "done")
        messages = stream.call_args.args[0]
        self.assertIn(previous.content, messages[1]["content"])
        self.assertIn("未沉淀，以正文为准", messages[1]["content"])
        self.assertEqual(repos.ChapterRepo(self.db).get(self.nid, previous.id).status, "draft")

    def test_continue_and_rewrite_only_include_prior_context(self):
        self.chapter(1, "过去已明确认识", status="finalized", summary="有效过去摘要")
        current = self.chapter(2, "本章待改写结尾", status="finalized", summary="本章摘要应排除")
        self.chapter(3, "未来章节正文应排除", status="finalized", summary="未来摘要应排除")
        for action in ("continue", "rewrite"):
            with self.subTest(action=action), patch.object(writing.llm, "stream_complete", return_value=iter(["新段落"])) as stream:
                operation = writing.continue_chapter(self.db, self.nid, current.id) if action == "continue" else writing.rewrite_passage(self.db, self.nid, current.id, "待改写", preview=True)
                self.collect(operation)
                prompt = stream.call_args.args[0][1]["content"]
                self.assertIn("过去已明确认识", prompt)
                self.assertIn("有效过去摘要", prompt)
                for text in ("本章摘要应排除", "未来摘要应排除", "未来章节正文应排除"):
                    self.assertNotIn(text, prompt)

    def test_autopilot_gap_prompt_carries_only_logical_prior_draft(self):
        nodes = self.nodes(3)
        self.chapter(1, "前章修过相框，约定下午查档案", outline_node_id=nodes[0].id)
        self.chapter(2, "未来仓库门口的正文", outline_node_id=nodes[2].id, status="finalized", summary="未来仓库摘要")
        with patch.object(auto_pilot.llm, "stream_complete", return_value=iter(["中间章正文"])) as stream:
            emitted = self.collect(auto_pilot.run_autopilot(self.nid, self.sessions))
        prompt = stream.call_args.args[0][1]["content"]
        self.assertIn("前章修过相框，约定下午查档案", prompt)
        self.assertIn("未定稿草稿", prompt)
        self.assertNotIn("未来仓库", prompt)
        self.assertEqual(emitted[-1]["generated"], 1)
        self.assertEqual([c.outline_node_id for c in repos.ChapterRepo(self.db).list(self.nid)], [n.id for n in nodes])

    def test_semantic_issue_pauses_after_save_without_failing_chapter(self):
        self.nodes(2)
        issue = StoryCheckIssue(category="directive", severity="medium", requirement="主角拒绝偷档案", excerpt="正常登记", reason="只写登记，没有提议偷档案与明确拒绝场景")
        with (
            patch.object(auto_pilot.llm, "stream_complete", side_effect=lambda *a, **kw: iter(["正常登记"])),
            patch.object(auto_pilot, "review_story", side_effect=[StoryCheckResult(issues=[issue]), StoryCheckResult()]) as review,
        ):
            stream = auto_pilot.run_autopilot(self.nid, self.sessions)
            self.addCleanup(stream.close)
            emitted = self.run_to_pause(stream)
            saved = repos.ChapterRepo(self.db).list(self.nid)
            self.assertEqual([(c.status, c.summary) for c in saved], [("draft", "")])
            self.assertTrue(any(e["type"] == "chapter_done" and e["generated"] == 1 and e["failed"] == 0 for e in emitted))
            check = next(e for e in emitted if e["type"] == "story_check")
            self.assertIn(issue.reason, check["warnings"][0])
            self.assertIn("正常登记", check["warnings"][0])
            self.assertIs(review.call_args.kwargs["provider"], auto_pilot.llm)
            self.registry.resume(self.nid)
            emitted.extend(self.collect(stream))
        self.assertEqual((emitted[-1]["status"], emitted[-1]["generated"], emitted[-1]["failed"]), ("completed", 2, 0))

    def test_resume_reads_author_repaired_prior_chapter_from_independent_session(self):
        self.nodes(3)
        old_scene = "第二章旧场景：只登记，没有任何提议。"
        corrected_scene = "第二章新场景：有人提议偷档案，林舟明确拒绝，并依法登记。"
        issue = StoryCheckIssue(category="directive", severity="medium", requirement="提议与拒绝场景",
                                excerpt="只登记", reason="需要补上提议和拒绝")
        reviewed = []

        def review_after_repair(*args, **kwargs):
            reviewed.append(kwargs["chapter_seq"])
            if kwargs["chapter_seq"] == 1:
                return StoryCheckResult()
            if kwargs["chapter_seq"] == 2:
                return StoryCheckResult(issues=[issue])
            return real_review_story(*args, **kwargs)

        with (
            patch.object(auto_pilot.llm, "stream_complete", side_effect=[
                iter(["第一章录音带前情。"]), iter([old_scene]), iter(["第三章继续依法查档案。"]),
            ]) as write_stream,
            patch.object(auto_pilot, "review_story", side_effect=review_after_repair) as review,
            patch.object(auto_pilot.llm, "complete", return_value='{"checks":[]}') as quality_call,
        ):
            stream = auto_pilot.run_autopilot(self.nid, self.sessions)
            self.addCleanup(stream.close)
            emitted = self.run_to_pause(stream)
            second = next(e for e in emitted if e["type"] == "chapter_done" and e["seq"] == 2)
            with self.sessions() as author:
                repos.ChapterRepo(author).update(self.nid, second["chapter_id"], content=corrected_scene)
            self.registry.resume(self.nid)
            emitted.extend(self.collect(stream))
        third_prompt = write_stream.call_args_list[2].args[0][1]["content"]
        self.assertIn(corrected_scene, third_prompt)
        self.assertNotIn(old_scene, third_prompt)
        quality_call.assert_called_once()
        third_review_prompt = quality_call.call_args.args[0][1]["content"]
        self.assertIn(corrected_scene, third_review_prompt)
        self.assertNotIn(old_scene, third_review_prompt)
        self.assertEqual(review.call_args_list[2].kwargs["chapter_seq"], 3)
        with self.sessions() as observer:
            saved = repos.ChapterRepo(observer).get(self.nid, second["chapter_id"])
            self.assertEqual((saved.content, saved.status, saved.summary), (corrected_scene, "draft", ""))
        self.assertEqual((emitted[-1]["status"], emitted[-1]["generated"], emitted[-1]["failed"]), ("completed", 3, 0))

    def test_author_context_refresh_rejects_pending_writes_without_discarding_them(self):
        chapter = self.chapter(1, "作者原稿")
        with patch.object(self.db, "expire_all") as expire:
            chapter.content = "仍待提交的作者改稿"
            with self.assertRaisesRegex(RuntimeError, "未提交写入"):
                auto_pilot._refresh_author_context(self.db)
            self.assertEqual(chapter.content, "仍待提交的作者改稿")
            expire.assert_not_called()
            self.db.rollback()
            self.db.add(models.Chapter(novel_id=self.nid, seq=2, title="待保存新章", content="待保存正文"))
            with self.assertRaisesRegex(RuntimeError, "未提交写入"):
                auto_pilot._refresh_author_context(self.db)
            self.assertEqual(len(self.db.new), 1)
            expire.assert_not_called()
            self.db.rollback()
            self.db.delete(chapter)
            with self.assertRaisesRegex(RuntimeError, "未提交写入"):
                auto_pilot._refresh_author_context(self.db)
            self.assertIn(chapter, self.db.deleted)
            expire.assert_not_called()
            self.db.rollback()

    def test_incomplete_review_pauses_and_keeps_generated_body(self):
        for result in (StoryCheckResult(review_complete=False), StoryCheckResult(truncated=True, review_complete=False)):
            with self.subTest(truncated=result.truncated):
                # 每组新作品，以免第一组已保存章节影响第二组路径。
                self.nid = repos.NovelRepo(self.db).create(title="复核未完成").id
                self.nodes(2)
                with patch.object(auto_pilot.llm, "stream_complete", return_value=iter(["完整保存正文"])), patch.object(auto_pilot, "review_story", return_value=result):
                    stream = auto_pilot.run_autopilot(self.nid, self.sessions)
                    emitted = self.run_to_pause(stream)
                    check = next(e for e in emitted if e["type"] == "story_check")
                    self.assertFalse(check["review_complete"])
                    self.assertTrue(check["warnings"])
                    self.assertEqual(repos.ChapterRepo(self.db).list(self.nid)[0].content, "完整保存正文")
                    self.registry.get(self.nid).stop = True
                    terminal = self.collect(stream)[-1]
                    self.assertEqual((terminal["generated"], terminal["failed"]), (1, 0))

    def test_review_error_keeps_last_chapter_and_hides_private_endpoint(self):
        self.nodes(1)
        with patch.object(auto_pilot.llm, "stream_complete", return_value=iter(["最终正文"])), patch.object(auto_pilot, "review_story", side_effect=RuntimeError("https://private-gateway.invalid/secret")):
            emitted = self.collect(auto_pilot.run_autopilot(self.nid, self.sessions))
        error = next(e for e in emitted if e["type"] == "error")
        self.assertEqual((error["scope"], error["kind"], error["saved"]), ("quality", "story_check", True))
        self.assertNotIn("private-gateway", json.dumps(emitted))
        self.assertEqual((emitted[-1]["status"], emitted[-1]["generated"], emitted[-1]["failed"]), ("completed", 1, 0))
        self.assertIn("警告", emitted[-1]["message"])
        self.assertNotIn("paused", [e["type"] for e in emitted])
        self.assertEqual(repos.ChapterRepo(self.db).list(self.nid)[0].content, "最终正文")

    def test_invalid_real_review_json_keeps_body_and_reconnected_warning(self):
        self.nodes(2)
        task_id = str(uuid.uuid4())
        task_repo = AutopilotTaskRepo(self.db)
        task_repo.create(self.nid, task_id)
        with (
            patch.object(auto_pilot.llm, "stream_complete", return_value=iter(["完整保存的正文"])),
            patch.object(auto_pilot.llm, "complete", return_value="无法返回合法JSON") as complete,
            patch.object(auto_pilot, "review_story", side_effect=real_review_story),
        ):
            stream = auto_pilot.run_autopilot(self.nid, self.sessions)
            emitted = self.run_to_pause(stream)
            complete.assert_called_once()
            for decoded in emitted:
                task_repo.append(self.nid, task_id, decoded)
            with self.sessions() as reconnect:
                snapshot = AutopilotTaskRepo(reconnect).get(self.nid, task_id)
            self.assertEqual((snapshot["status"], snapshot["generated"], snapshot["failed"]), ("paused", 1, 0))
            self.assertEqual(len(snapshot["quality_warnings"]), 1)
            self.assertIn("正文已保存，剧情复核未能完成", snapshot["quality_warnings"][0])
            self.assertEqual([(c.content, c.status, c.summary) for c in repos.ChapterRepo(self.db).list(self.nid)], [("完整保存的正文", "draft", "")])
            self.registry.get(self.nid).stop = True
            self.collect(stream)

    def test_rule_conflicts_pause_after_save_and_persist_warning_for_reconnect(self):
        self.nodes(2)
        repos.CharacterRepo(self.db).create(self.nid, name="林舟", known_info="不知道失踪者身份")
        task_id = str(uuid.uuid4())
        task_repo = AutopilotTaskRepo(self.db)
        task_repo.create(self.nid, task_id)
        with patch.object(auto_pilot.llm, "stream_complete", return_value=iter(["林舟知道失踪者身份。"])), patch.object(auto_pilot, "review_story", return_value=StoryCheckResult()):
            stream = auto_pilot.run_autopilot(self.nid, self.sessions)
            emitted = self.run_to_pause(stream)
            for decoded in emitted:
                task_repo.append(self.nid, task_id, decoded)
            with self.sessions() as reconnect:
                snapshot = AutopilotTaskRepo(reconnect).get(self.nid, task_id)
            self.assertEqual((snapshot["status"], snapshot["generated"], snapshot["failed"]), ("paused", 1, 0))
            self.assertIn("知道了不该知道的信息", snapshot["quality_warnings"][0])
            self.assertIn("失踪者身份", snapshot["quality_warnings"][0])
            self.assertEqual(len(repos.ChapterRepo(self.db).list(self.nid)), 1)
            self.registry.get(self.nid).stop = True
            for decoded in self.collect(stream):
                task_repo.append(self.nid, task_id, decoded)
            self.assertEqual(task_repo.get(self.nid, task_id)["quality_warnings"], snapshot["quality_warnings"])

    def test_bounded_history_scope_does_not_pause_successful_review(self):
        self.nodes(1)
        with patch.object(auto_pilot.llm, "stream_complete", return_value=iter(["正文"])), patch.object(auto_pilot, "review_story", return_value=StoryCheckResult(history_truncated=True)):
            emitted = self.collect(auto_pilot.run_autopilot(self.nid, self.sessions))
        check = next(e for e in emitted if e["type"] == "story_check")
        self.assertTrue(check["history_truncated"])
        self.assertTrue(check["review_complete"])
        self.assertFalse(check["warnings"])
        self.assertEqual(emitted[-1]["status"], "completed")


# 保存真实实现用于召回过滤测试；所有搜索结果均由mock给出。
DraftContinuityTest._original_recall = staticmethod(context._semantic_recall)


if __name__ == "__main__":
    unittest.main()
