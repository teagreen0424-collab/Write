"""阶段二单元测试：build_context / settle_chapter / consistency_check。"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.application import consistency as consistency_uc
from app.application import context as context_uc
from app.application import settle as settle_uc
from app.application.memory import MemoryWriteResult
from app.application import writing as writing_uc
from app.infrastructure import models
from app.infrastructure import repository as repos
from app.main import app

engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
models.Base.metadata.create_all(bind=engine)
Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
client = TestClient(app)


def _parse_sse(stream) -> list[dict]:
    """把 SSE 文本行解析为事件字典。"""
    events = []
    for line in stream:
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: ") :]))
    return events


class WritingUseCaseTest(unittest.TestCase):
    def setUp(self):
        self.db = Session()
        novel = repos.NovelRepo(self.db).create(title="测试作品", genre="玄幻", summary="")
        self.novel_id = novel.id

    def tearDown(self):
        self.db.close()

    def _add_character(self, name, known_info=""):
        return repos.CharacterRepo(self.db).create(self.novel_id, name=name, known_info=known_info)

    def test_build_context_priority(self):
        self._add_character("林越", known_info="青云宗弟子")
        repos.ChapterRepo(self.db).create(
            self.novel_id, seq=1, title="第一章", content="...", status="finalized", summary="林越入门"
        )
        text = context_uc.build_context(
            self.db, self.novel_id, directive="让林越与苏婉儿决裂", chapter_position="第 2 章"
        )
        self.assertIn("让林越与苏婉儿决裂", text)
        self.assertIn("林越", text)
        self.assertIn("林越入门", text)

    def test_consistency_foreshadowing(self):
        repos.ForeshadowingRepo(self.db).create(
            self.novel_id, content="身世之谜", status="consumed"
        )
        result = consistency_uc.consistency_check(self.db, self.novel_id, "他忽然想起身世之谜")
        self.assertTrue(any(c["rule"] == "foreshadowing" for c in result["conflicts"]))

    def test_consistency_known_info(self):
        self._add_character("林越", known_info="不知道自己是青云宗遗孤")
        result = consistency_uc.consistency_check(self.db, self.novel_id, "林越得知自己是青云宗遗孤")
        self.assertTrue(any(c["rule"] == "known_info" for c in result["conflicts"]))

    def test_settle_chapter(self):
        chapter = repos.ChapterRepo(self.db).create(
            self.novel_id, seq=1, title="第一章", content="林越在青云山下捡到一枚玉佩。"
        )
        fake = (
            '{"summary":"林越捡到玉佩",'
            '"characters":[{"name":"苏婉儿","role":"配角"}],'
            '"events":[],'
            '"foreshadowings":[{"content":"玉佩来历","status":"planted"}]}'
        )
        with (
            patch("app.application.settle.llm.complete", return_value=fake),
            patch("app.application.settle.write_chapter_memory", return_value=MemoryWriteResult(0, 0, 0, "empty")),
        ):
            result = settle_uc.settle_chapter(self.db, self.novel_id, chapter.id)

        self.assertEqual(result["summary"], "林越捡到玉佩")
        self.assertEqual(len(result["suggestions"]), 2)
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["checks"], {"drift": "skipped", "consistency": "completed"})
        updated = repos.ChapterRepo(self.db).get(self.novel_id, chapter.id)
        self.assertEqual(updated.status, "finalized")

    def test_apply_suggestions(self):
        suggestions = [
            {"type": "character", "data": {"name": "苏婉儿", "role": "配角"}},
            {"type": "foreshadowing", "data": {"content": "玉佩来历", "status": "planted"}},
        ]
        result = settle_uc.apply_suggestions(self.db, self.novel_id, suggestions)
        self.assertEqual(len(result["created"]), 2)
        self.assertEqual(len(repos.CharacterRepo(self.db).list(self.novel_id)), 1)
        self.assertEqual(len(repos.ForeshadowingRepo(self.db).list(self.novel_id)), 1)

    def test_rewrite_preview_not_persisted(self):
        chapter = repos.ChapterRepo(self.db).create(
            self.novel_id, seq=1, title="第一章", content="开头。原文段落内容。结尾。"
        )
        with patch(
            "app.application.writing.llm.stream_complete", return_value=iter(["改写后"])
        ):
            events = _parse_sse(
                writing_uc.rewrite_passage(
                    self.db, self.novel_id, chapter.id, "原文段落内容", preview=True
                )
            )

        self.assertFalse(any(e["type"] == "error" for e in events))
        done = next(e for e in events if e["type"] == "done")
        self.assertTrue(done["preview"])
        self.assertEqual(done["content"], "开头。改写后。结尾。")
        # 预览不落库：正文保持原文
        self.assertEqual(
            repos.ChapterRepo(self.db).get(self.novel_id, chapter.id).content,
            "开头。原文段落内容。结尾。",
        )

    def test_rewrite_default_persists(self):
        chapter = repos.ChapterRepo(self.db).create(
            self.novel_id, seq=1, title="第一章", content="开头。原文段落内容。结尾。"
        )
        with patch(
            "app.application.writing.llm.stream_complete", return_value=iter(["改写后"])
        ):
            events = _parse_sse(
                writing_uc.rewrite_passage(
                    self.db, self.novel_id, chapter.id, "原文段落内容"
                )
            )

        done = next(e for e in events if e["type"] == "done")
        self.assertNotIn("preview", done)
        self.assertEqual(
            repos.ChapterRepo(self.db).get(self.novel_id, chapter.id).content,
            "开头。改写后。结尾。",
        )

    def test_write_route_persists_outline_node_id(self):
        volume = repos.OutlineRepo(self.db).create(
            self.novel_id, title="第一卷", level="volume"
        )
        outline = repos.OutlineRepo(self.db).create(
            self.novel_id, title="第一章", level="chapter",
            parent_id=volume.id, summary="本章必须出现的剧情要点",
        )
        with (
            patch("app.interfaces.api.v1.chapters.SessionLocal", side_effect=Session),
            patch("app.application.writing.llm.stream_complete", return_value=iter(["正文"])) as stream,
        ):
            response = client.post(
                f"/api/v1/novels/{self.novel_id}/chapters/write",
                json={"title": "第一章", "outline_node_id": outline.id},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('"type": "done"', response.text)
        chapters = repos.ChapterRepo(self.db).list(self.novel_id)
        self.assertEqual(len(chapters), 1)
        self.assertEqual(chapters[0].outline_node_id, outline.id)
        prompt = stream.call_args.args[0][1]["content"]
        self.assertIn("【本章大纲目标（尚未发生，必须推进）】", prompt)
        self.assertIn("本章必须出现的剧情要点", prompt)
        self.assertLess(prompt.index("【本章大纲目标"), prompt.index("请开始撰写本章正文"))

    def test_write_route_rejects_non_chapter_outline_node(self):
        volume = repos.OutlineRepo(self.db).create(
            self.novel_id, title="第一卷", level="volume"
        )
        with (
            patch("app.interfaces.api.v1.chapters.SessionLocal", side_effect=Session),
            patch("app.application.writing.llm.stream_complete") as stream,
        ):
            response = client.post(
                f"/api/v1/novels/{self.novel_id}/chapters/write",
                json={"outline_node_id": volume.id},
            )
        self.assertIn("只能绑定章级大纲节点", response.text)
        stream.assert_not_called()
        self.assertEqual(repos.ChapterRepo(self.db).list(self.novel_id), [])

    def test_write_route_without_outline_node_id_keeps_empty_value(self):
        with (
            patch("app.interfaces.api.v1.chapters.SessionLocal", side_effect=Session),
            patch("app.application.writing.llm.stream_complete", return_value=iter(["正文"])),
        ):
            response = client.post(
                f"/api/v1/novels/{self.novel_id}/chapters/write",
                json={"title": "第一章"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        chapters = repos.ChapterRepo(self.db).list(self.novel_id)
        self.assertEqual(len(chapters), 1)
        self.assertEqual(chapters[0].outline_node_id, "")

    def test_long_directive_does_not_hide_selected_outline_target(self):
        volume = repos.OutlineRepo(self.db).create(
            self.novel_id, title="第一卷", level="volume"
        )
        outline = repos.OutlineRepo(self.db).create(
            self.novel_id, title="第二章", level="chapter", parent_id=volume.id,
            summary="落地后必须闻到消毒水混着铁锈的气味",
        )
        previous = repos.ChapterRepo(self.db).create(
            self.novel_id, seq=1, title="第一章", content="上一章结尾还挂在树上", status="draft"
        )
        with patch.object(writing_uc.llm, "stream_complete", return_value=iter(["正文"])) as stream:
            events = _parse_sse(writing_uc.write_chapter(
                self.db, self.novel_id, directive="长指令" * 1200,
                outline_node_id=outline.id, previous_chapter_id=previous.id,
            ))

        self.assertEqual(events[-1]["type"], "done")
        prompt = stream.call_args.args[0][1]["content"]
        self.assertIn("落地后必须闻到消毒水混着铁锈的气味", prompt)
        self.assertLess(prompt.index("上一章结尾还挂在树上"), prompt.index("【本章大纲目标"))
        self.assertLess(prompt.index("【本章大纲目标"), prompt.index("【续章硬约束】"))

    def test_write_next_chapter_includes_continuity_context(self):
        volume = repos.OutlineRepo(self.db).create(
            self.novel_id, title="第一卷", level="volume"
        )
        outline = repos.OutlineRepo(self.db).create(
            self.novel_id, title="第二章", level="chapter", parent_id=volume.id,
            summary="落地后有消毒水混着铁锈的气味",
        )
        previous = repos.ChapterRepo(self.db).create(
            self.novel_id,
            seq=1,
            title="雨夜相遇",
            content="章首身份：林越是外卖员。" + "中段经历" * 2500 + "上一章结尾线索",
            status="draft",
        )
        with (
            patch("app.interfaces.api.v1.chapters.SessionLocal", side_effect=Session),
            patch(
                "app.application.writing.llm.stream_complete",
                return_value=iter(["承接后的第二章正文"]),
            ) as stream,
        ):
            response = client.post(
                f"/api/v1/novels/{self.novel_id}/chapters/write",
                json={"previous_chapter_id": previous.id, "outline_node_id": outline.id},
            )

        self.assertEqual(response.status_code, 200, response.text)
        messages = stream.call_args.args[0]
        self.assertIn("根据给出的上一章保持剧情连续", messages[0]["content"])
        self.assertIn("Story Bible 的明确事实是最高依据", messages[0]["content"])
        self.assertIn("上一章草稿可能有错，不能反过来覆盖设定", messages[0]["content"])
        self.assertIn("【必须直接承接的上一章】", messages[1]["content"])
        self.assertIn("章首身份：林越是外卖员", messages[1]["content"])
        self.assertIn("上一章中段已省略", messages[1]["content"])
        self.assertIn("上一章结尾线索", messages[1]["content"])
        self.assertIn("不得再次写成第一次", messages[1]["content"])
        self.assertIn("未沉淀，以正文为准", messages[1]["content"])
        self.assertIn("落地后有消毒水混着铁锈的气味", messages[1]["content"])
        self.assertLess(
            messages[1]["content"].index("上一章结尾线索"),
            messages[1]["content"].index("【本章大纲目标"),
        )
        self.assertIn("具体人物、物件、气味、数字、地点及因果线索不可随意替换", messages[1]["content"])
        self.assertIn("大纲场面与前章存在差别时保持明确设定，写出合理衔接原因", messages[0]["content"])
        chapters = repos.ChapterRepo(self.db).list(self.novel_id)
        self.assertEqual(len(chapters), 2)
        self.assertEqual(chapters[1].seq, 2)
        self.assertEqual(chapters[1].title, "第2章")
        self.assertEqual(chapters[1].content, "承接后的第二章正文")
        self.assertEqual(chapters[1].outline_node_id, outline.id)

    def test_write_next_chapter_rejects_non_latest_previous_chapter(self):
        first = repos.ChapterRepo(self.db).create(
            self.novel_id, seq=1, title="第一章", content="第一章正文"
        )
        repos.ChapterRepo(self.db).create(
            self.novel_id, seq=2, title="第二章", content="第二章正文"
        )
        with (
            patch("app.interfaces.api.v1.chapters.SessionLocal", side_effect=Session),
            patch("app.application.writing.llm.stream_complete") as stream,
        ):
            response = client.post(
                f"/api/v1/novels/{self.novel_id}/chapters/write",
                json={"previous_chapter_id": first.id},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("只能从最新章节生成下一章", response.text)
        stream.assert_not_called()
        self.assertEqual(len(repos.ChapterRepo(self.db).list(self.novel_id)), 2)

    def test_delete_chapter_cleans_memory_and_foreshadowing_references(self):
        chapter = repos.ChapterRepo(self.db).create(
            self.novel_id, seq=1, title="第一章", content="将被删除的正文"
        )
        repos.MemoryChunkRepo(self.db).create(
            self.novel_id,
            kind="chapter_slice",
            source_chapter_id=chapter.id,
            text="派生记忆",
            embedding_id="",
            vector=[],
        )
        foreshadowing = repos.ForeshadowingRepo(self.db).create(
            self.novel_id,
            content="仍需保留的伏笔",
            planted_chapter_id=chapter.id,
            consumed_chapter_id=chapter.id,
        )

        repos.ChapterRepo(self.db).delete(self.novel_id, chapter.id)

        self.assertEqual(repos.ChapterRepo(self.db).list(self.novel_id), [])
        memories = repos.MemoryChunkRepo(self.db).list_by_novel(self.novel_id)
        self.assertFalse(any(item.source_chapter_id == chapter.id for item in memories))
        kept = repos.ForeshadowingRepo(self.db).get(self.novel_id, foreshadowing.id)
        self.assertEqual(kept.planted_chapter_id, "")
        self.assertEqual(kept.consumed_chapter_id, "")

    def test_settle_with_anchor_calls_extraction_and_drift_once(self):
        repos.NovelRepo(self.db).update(self.novel_id, style_anchor="简练古风")
        chapter = repos.ChapterRepo(self.db).create(
            self.novel_id, seq=1, title="第一章", content="林越在青云山下捡到一枚玉佩。"
        )
        fake = (
            '{"summary":"林越捡到玉佩","characters":[],'
            '"events":[],"foreshadowings":[]}'
        )
        with (
            patch("app.application.settle.llm.complete", return_value=fake) as extraction,
            patch("app.application.settle.write_chapter_memory", return_value=MemoryWriteResult(0, 0, 0, "empty")),
            patch("app.application.settle.check_drift", return_value=0.42) as drift,
            patch("app.application.consistency.deep_check") as deep_check,
        ):
            result = settle_uc.settle_chapter(self.db, self.novel_id, chapter.id)

        extraction.assert_called_once()
        drift.assert_called_once_with(self.db, self.novel_id, chapter.content)
        deep_check.assert_not_called()
        self.assertEqual(result["drift"], 0.42)
        self.assertEqual(result["checks"]["drift"], "completed")
        self.assertEqual(result["warnings"], [])
        self.assertEqual(
            repos.ChapterRepo(self.db).get(self.novel_id, chapter.id).drift_score, 0.42
        )

    def test_settle_without_anchor_skips_drift_and_deep_check(self):
        chapter = repos.ChapterRepo(self.db).create(
            self.novel_id, seq=1, title="第一章", content="林越走进青云山。"
        )
        fake = (
            '{"summary":"林越进山","characters":[],'
            '"events":[],"foreshadowings":[]}'
        )
        with (
            patch("app.application.settle.llm.complete", return_value=fake) as extraction,
            patch("app.application.settle.write_chapter_memory", return_value=MemoryWriteResult(0, 0, 0, "empty")),
            patch("app.application.settle.check_drift") as drift,
            patch("app.application.consistency.deep_check") as deep_check,
        ):
            result = settle_uc.settle_chapter(self.db, self.novel_id, chapter.id)

        extraction.assert_called_once()
        drift.assert_not_called()
        deep_check.assert_not_called()
        self.assertIsNone(result["drift"])

    def test_settle_invalid_extraction_is_rejected_before_any_saved_result(self):
        chapter = repos.ChapterRepo(self.db).create(
            self.novel_id, title="待定稿章", content="正文", status="draft", summary="原摘要"
        )
        repos.MemoryChunkRepo(self.db).create(
            self.novel_id, kind="chapter_slice", source_chapter_id=chapter.id, text="旧记忆", vector=[1.0]
        )
        malformed = ["[]", "不是JSON", '{"summary":42}', '{"characters":null}',
                     '{"events":{}}', '{"foreshadowings":[{"content":42}]}']
        for raw in malformed:
            with (
                self.subTest(raw=raw),
                patch.object(settle_uc.llm, "complete", return_value=raw),
                patch.object(settle_uc, "write_chapter_memory") as memory_write,
            ):
                with self.assertRaises(HTTPException) as error:
                    settle_uc.settle_chapter(self.db, self.novel_id, chapter.id)
            self.assertEqual(error.exception.status_code, 502)
            memory_write.assert_not_called()
            current = repos.ChapterRepo(self.db).get(self.novel_id, chapter.id)
            self.assertEqual((current.status, current.summary), ("draft", "原摘要"))
            self.assertEqual([c.text for c in repos.MemoryChunkRepo(self.db).list_by_novel(self.novel_id)], ["旧记忆"])

    def test_settle_extraction_service_failure_preserves_original_chapter(self):
        chapter = repos.ChapterRepo(self.db).create(self.novel_id, title="第一章", content="正文")
        with patch.object(settle_uc.llm, "complete", side_effect=RuntimeError("服务断开")):
            with self.assertRaises(HTTPException) as error:
                settle_uc.settle_chapter(self.db, self.novel_id, chapter.id)
        self.assertEqual(error.exception.status_code, 502)
        self.assertEqual(repos.ChapterRepo(self.db).get(self.novel_id, chapter.id).status, "draft")

    def test_settle_drift_failure_reports_incomplete_check_and_preserves_saved_extraction(self):
        repos.NovelRepo(self.db).update(self.novel_id, style_anchor="简练古风")
        chapter = repos.ChapterRepo(self.db).create(self.novel_id, title="第一章", content="正文", drift_score=0.3)
        with (
            patch.object(settle_uc.llm, "complete", return_value='{"summary":"已保存摘要","characters":[{"name":"新人物"}]}') as extraction,
            patch.object(settle_uc, "write_chapter_memory", return_value=MemoryWriteResult(1, 1, 0, "saved")),
            patch.object(settle_uc, "check_drift", side_effect=RuntimeError("漂移服务断开")) as drift,
            patch.object(consistency_uc, "deep_check") as deep_check,
        ):
            result = settle_uc.settle_chapter(self.db, self.novel_id, chapter.id)
        extraction.assert_called_once()
        drift.assert_called_once()
        deep_check.assert_not_called()
        self.assertEqual(result["checks"], {"drift": "failed", "consistency": "completed"})
        self.assertEqual(result["drift"], None)
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("漂移检查未完成", result["warnings"][0])
        self.assertEqual(result["suggestions"][0]["data"]["name"], "新人物")
        saved = repos.ChapterRepo(self.db).get(self.novel_id, chapter.id)
        self.assertEqual((saved.status, saved.summary, saved.drift_score), ("finalized", "已保存摘要", None))

    def test_settle_failed_rule_check_does_not_claim_absence_of_conflicts(self):
        chapter = repos.ChapterRepo(self.db).create(self.novel_id, title="第一章", content="正文")
        with (
            patch.object(settle_uc.llm, "complete", return_value='{"summary":"已保存摘要"}'),
            patch.object(settle_uc, "write_chapter_memory", return_value=MemoryWriteResult(1, 1, 0, "saved")),
            patch.object(settle_uc, "consistency_check", side_effect=RuntimeError("规则读取失败")),
        ):
            result = settle_uc.settle_chapter(self.db, self.novel_id, chapter.id)
        self.assertEqual(result["checks"], {"drift": "skipped", "consistency": "failed"})
        self.assertEqual(result["conflicts"], [])
        self.assertIn("不能确认没有冲突", result["warnings"][0])
        self.assertEqual(repos.ChapterRepo(self.db).get(self.novel_id, chapter.id).summary, "已保存摘要")

    def test_settle_memory_source_read_failure_preserves_saved_result_and_reports_warning(self):
        chapter = repos.ChapterRepo(self.db).create(self.novel_id, title="第一章", content="正文")
        with (
            patch.object(settle_uc.llm, "complete", return_value='{"summary":"已保存摘要"}'),
            patch.object(settle_uc, "write_chapter_memory", side_effect=RuntimeError("记忆来源读取失败")),
        ):
            result = settle_uc.settle_chapter(self.db, self.novel_id, chapter.id)
        self.assertEqual(result["memory"], {"written": 0, "total": 1, "failed": 1, "status": "storage_failed"})
        self.assertIn("记忆沉淀未完成", result["warnings"][0])
        self.assertEqual(repos.ChapterRepo(self.db).get(self.novel_id, chapter.id).summary, "已保存摘要")

    def test_settle_rejects_concurrent_author_save_using_database_snapshot_before_memory(self):
        from app.application import revisions

        with TemporaryDirectory(prefix="lingmo-settle-race-") as directory:
            local_engine = create_engine(
                f"sqlite:///{(Path(directory) / 'race.db').as_posix()}",
                connect_args={"check_same_thread": False},
            )
            models.Base.metadata.create_all(bind=local_engine)
            LocalSession = sessionmaker(bind=local_engine, autoflush=False)
            try:
                for field, value in (("content", "并发保存的新正文"), ("title", "并发保存的新标题"), ("directive", "并发保存的新指令")):
                    with self.subTest(field=field), LocalSession() as db:
                        novel = repos.NovelRepo(db).create(title="并发定稿作品")
                        novel_id = novel.id
                        chapter = repos.ChapterRepo(db).create(
                            novel_id, title="原标题", content="原正文", directive="原指令", summary="原摘要"
                        )
                        chapter_id = chapter.id
                        original = {key: getattr(chapter, key) for key in ("title", "content", "directive")}
                        repos.MemoryChunkRepo(db).create(
                            novel_id, kind="chapter_slice", source_chapter_id=chapter_id, text="原正文", vector=[1.0]
                        )
                        extracting, saved = Event(), Event()
                        errors = []

                        def save_during_extraction():
                            try:
                                if not extracting.wait(5):
                                    raise AssertionError("模型抽取未开始")
                                with LocalSession() as other:
                                    revisions.update_chapter_with_revision(
                                        other, novel_id, chapter_id, {field: value}, revisions.SOURCE_MANUAL_SAVE
                                    )
                            except Exception as error:
                                errors.append(error)
                            finally:
                                saved.set()

                        def extraction(*args, **kwargs):
                            extracting.set()
                            if not saved.wait(5) or errors:
                                raise AssertionError(f"并发保存失败：{errors}")
                            return '{"summary":"基于原稿的旧摘要","characters":[{"name":"旧稿人物"}]}'

                        writer = Thread(target=save_during_extraction, daemon=True)
                        writer.start()
                        try:
                            with (
                                patch.object(settle_uc.llm, "complete", side_effect=extraction) as model,
                                patch.object(settle_uc, "write_chapter_memory") as memory_write,
                            ):
                                with self.assertRaises(HTTPException) as error:
                                    settle_uc.settle_chapter(db, novel_id, chapter_id)
                        finally:
                            writer.join(5)
                        self.assertFalse(writer.is_alive())
                        self.assertEqual(errors, [])
                        self.assertEqual(error.exception.status_code, 409)
                        model.assert_called_once()
                        memory_write.assert_not_called()
                        db.expire_all()
                        current = repos.ChapterRepo(db).get(novel_id, chapter_id)
                        original[field] = value
                        self.assertEqual({key: getattr(current, key) for key in original}, original)
                        self.assertEqual((current.status, current.summary), ("draft", ""))
                        self.assertEqual(repos.MemoryChunkRepo(db).list_by_novel(novel_id), [])
                        self.assertEqual(db.query(models.ChapterRevision).filter_by(chapter_id=chapter_id).count(), 1)
                        self.assertEqual(db.query(models.ChapterRevision).filter_by(chapter_id=chapter_id).one().summary, "原摘要")
            finally:
                local_engine.dispose()

    def test_settle_allows_concurrent_derived_update_when_author_snapshot_is_unchanged(self):
        chapter = repos.ChapterRepo(self.db).create(self.novel_id, title="第一章", content="正文")
        chapter_id = chapter.id

        def extraction(*args, **kwargs):
            with Session() as other:
                repos.ChapterRepo(other).update(self.novel_id, chapter_id, tension_score=0.7)
            return '{"summary":"当前正文摘要"}'

        with (
            patch.object(settle_uc.llm, "complete", side_effect=extraction) as model,
            patch.object(settle_uc, "write_chapter_memory", return_value=MemoryWriteResult(1, 1, 0, "saved")),
        ):
            result = settle_uc.settle_chapter(self.db, self.novel_id, chapter_id)
        model.assert_called_once()
        self.assertEqual(result["summary"], "当前正文摘要")
        saved = repos.ChapterRepo(self.db).get(self.novel_id, chapter_id)
        self.assertEqual((saved.status, saved.tension_score), ("finalized", 0.7))

    def test_settle_drift_write_rejects_changed_author_source_and_preserves_new_score(self):
        repos.NovelRepo(self.db).update(self.novel_id, style_anchor="简练古风")
        for field, value in (("content", "后置检查期间保存的新正文"), ("title", "新标题"), ("directive", "新指令")):
            with self.subTest(field=field):
                chapter = repos.ChapterRepo(self.db).create(
                    self.novel_id, title="原标题", content="原正文", directive="原指令", drift_score=0.3
                )
                chapter_id = chapter.id

                def concurrent_save(*args, **kwargs):
                    with Session() as other:
                        repos.ChapterRepo(other).update(
                            self.novel_id, chapter_id, **{field: value},
                            status="draft", summary="新稿摘要", drift_score=0.9,
                        )
                    return 0.42

                with (
                    patch.object(settle_uc.llm, "complete", return_value='{"summary":"原稿摘要"}') as extraction,
                    patch.object(settle_uc, "write_chapter_memory", return_value=MemoryWriteResult(1, 1, 0, "saved")),
                    patch.object(settle_uc, "check_drift", side_effect=concurrent_save) as drift,
                ):
                    result = settle_uc.settle_chapter(self.db, self.novel_id, chapter_id)
                extraction.assert_called_once()
                drift.assert_called_once()
                self.assertEqual(result["checks"]["drift"], "failed")
                self.assertIsNone(result["drift"])
                self.assertIn("旧稿评分未写入", result["warnings"][0])
                self.db.expire_all()
                saved = repos.ChapterRepo(self.db).get(self.novel_id, chapter_id)
                self.assertEqual(getattr(saved, field), value)
                self.assertEqual((saved.status, saved.summary, saved.drift_score), ("draft", "新稿摘要", 0.9))

    def test_settle_without_anchor_invalidates_previous_drift_score(self):
        chapter = repos.ChapterRepo(self.db).create(self.novel_id, title="第一章", content="正文", drift_score=0.8)
        with (
            patch.object(settle_uc.llm, "complete", return_value='{"summary":"本轮摘要"}') as extraction,
            patch.object(settle_uc, "write_chapter_memory", return_value=MemoryWriteResult(1, 1, 0, "saved")),
            patch.object(settle_uc, "check_drift") as drift,
        ):
            result = settle_uc.settle_chapter(self.db, self.novel_id, chapter.id)
        extraction.assert_called_once()
        drift.assert_not_called()
        self.assertEqual(result["checks"]["drift"], "skipped")
        self.assertEqual(result["warnings"], [])
        self.assertIsNone(result["drift"])
        self.assertIsNone(repos.ChapterRepo(self.db).get(self.novel_id, chapter.id).drift_score)


if __name__ == "__main__":
    unittest.main()
