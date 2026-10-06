"""方向 B 首轮：大纲预览、原子追加和聊天动作去重。"""
import unittest
import json
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.application import chat_actions, outline_plan, inject_idea, setting_index
from app.application import writing
from app.infrastructure import models
from app.infrastructure.repository import ConversationRepo, NovelRepo, OutlineRepo
from app.infrastructure.repository import ChapterRepo
from app.interfaces.schemas import OutlineVolumeDraft, WriteChapterRequest


def events(stream):
    return [json.loads(line.removeprefix("data: ")) for line in stream]


class OutlinePlanTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        models.Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine, autoflush=False)()
        self.novel = NovelRepo(self.db).create(title="测试作品")
        self.conv = ConversationRepo(self.db).create(novel_id=self.novel.id)
        self.message = ConversationRepo(self.db).add_message(self.conv.id, "assistant", "主角入城调查")
        self.plan = OutlineVolumeDraft.model_validate({
            "title": "第一卷", "summary": "入城",
            "chapters": [{"title": "第一章", "summary": "到达"}],
            "acts": [{"title": "第一幕", "summary": "调查", "chapters": [
                {"title": "第二章", "summary": "线索"},
            ]}],
        })

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_preview_calls_model_once_and_does_not_write(self):
        response = self.plan.model_dump_json()
        with patch.object(outline_plan.llm, "complete", return_value=response) as complete:
            preview = chat_actions.preview_outline(self.db, self.conv.id, self.message.id)
        self.assertEqual(preview.title, "第一卷")
        self.assertEqual(complete.call_count, 1)
        self.assertEqual(OutlineRepo(self.db).list_all(self.novel.id), [])
        self.assertEqual(self.db.query(models.ChatAction).count(), 0)

    def test_apply_appends_one_volume_with_hierarchy_and_is_idempotent(self):
        first = chat_actions.apply_outline(self.db, self.conv.id, self.message.id, self.plan)
        second = chat_actions.apply_outline(self.db, self.conv.id, self.message.id, self.plan)
        self.assertEqual(first, second)
        nodes = OutlineRepo(self.db).list_all(self.novel.id)
        self.assertEqual(len(nodes), 4)
        volume = next(n for n in nodes if n.level == "volume")
        act = next(n for n in nodes if n.level == "act")
        direct = next(n for n in nodes if n.title == "第一章")
        nested = next(n for n in nodes if n.title == "第二章")
        self.assertEqual(act.parent_id, volume.id)
        self.assertEqual(direct.parent_id, volume.id)
        self.assertEqual(nested.parent_id, act.id)
        self.assertEqual(len(chat_actions.list_actions(self.db, self.conv.id, self.message.id)), 1)

    def test_existing_outline_is_not_overwritten(self):
        old = OutlineRepo(self.db).create(self.novel.id, level="volume", title="旧卷")
        result = outline_plan.apply_outline(self.db, self.novel.id, self.plan)
        self.assertNotEqual(result["volume_id"], old.id)
        self.assertEqual(OutlineRepo(self.db).get(self.novel.id, old.id).title, "旧卷")
        new = OutlineRepo(self.db).get(self.novel.id, result["volume_id"])
        self.assertGreater(new.sort_order, old.sort_order)

    def test_global_or_user_message_cannot_apply(self):
        global_conv = ConversationRepo(self.db).create()
        global_message = ConversationRepo(self.db).add_message(global_conv.id, "assistant", "提议")
        with self.assertRaises(HTTPException) as global_error:
            chat_actions.apply_outline(self.db, global_conv.id, global_message.id, self.plan)
        self.assertEqual(global_error.exception.status_code, 400)
        user_message = ConversationRepo(self.db).add_message(self.conv.id, "user", "提议")
        with self.assertRaises(HTTPException) as user_error:
            chat_actions.apply_outline(self.db, self.conv.id, user_message.id, self.plan)
        self.assertEqual(user_error.exception.status_code, 404)
        self.assertEqual(OutlineRepo(self.db).list_all(self.novel.id), [])

    def test_action_cascades_when_conversation_deleted(self):
        chat_actions.apply_outline(self.db, self.conv.id, self.message.id, self.plan)
        ConversationRepo(self.db).delete(self.conv.id)
        self.assertEqual(self.db.query(models.ChatAction).count(), 0)

    def test_failed_apply_rolls_back_action_and_all_nodes(self):
        original = outline_plan.add_outline_nodes

        def fail_after_nodes(db, novel_id, plan):
            original(db, novel_id, plan)
            raise RuntimeError("模拟提交前失败")

        with patch.object(chat_actions.outline_plan, "add_outline_nodes", side_effect=fail_after_nodes):
            with self.assertRaises(RuntimeError):
                chat_actions.apply_outline(self.db, self.conv.id, self.message.id, self.plan)
        self.assertEqual(self.db.query(models.ChatAction).count(), 0)
        self.assertEqual(OutlineRepo(self.db).list_all(self.novel.id), [])

    def test_blank_or_empty_plan_is_rejected(self):
        for data in ({"title": " ", "chapters": [{"title": "第一章"}]},
                     {"title": "第一卷", "chapters": []}):
            with self.assertRaises(ValueError):
                OutlineVolumeDraft.model_validate(data)

    def test_message_meta_patch_merges_existing_fields(self):
        repo = ConversationRepo(self.db)
        repo.update_message(self.conv.id, self.message.id, {"applied": [1]})
        updated = repo.update_message(self.conv.id, self.message.id, {"note": "保留"})
        self.assertEqual(updated.meta, {"applied": [1], "note": "保留"})

    def test_chat_write_creates_one_draft_and_retry_does_not_call_model(self):
        request = WriteChapterRequest(title="新章", directive="主角入城")
        with patch.object(writing.llm, "stream_complete", return_value=iter(["正文"])) as complete:
            first = events(chat_actions.write_chapter(self.db, self.conv.id, self.message.id, request))
        self.assertEqual(first[-1]["type"], "done")
        chapter_id = first[-1]["chapter"]["id"]
        self.assertEqual(first[-1]["chapter"]["status"], "draft")
        with patch.object(writing.llm, "stream_complete") as complete_again:
            second = events(chat_actions.write_chapter(self.db, self.conv.id, self.message.id, request))
        complete_again.assert_not_called()
        self.assertEqual(second[-1]["chapter"]["id"], chapter_id)
        self.assertEqual(len(ChapterRepo(self.db).list(self.novel.id)), 1)
        self.assertEqual(len(chat_actions.list_actions(self.db, self.conv.id, self.message.id)), 1)
        ChapterRepo(self.db).create(
            self.novel.id, seq=2, title="后续章节", content="后文", status="draft",
        )
        with patch.object(writing.llm, "stream_complete") as complete_after_newer:
            third = events(chat_actions.write_chapter(self.db, self.conv.id, self.message.id, request))
        complete_after_newer.assert_not_called()
        self.assertEqual(third[-1]["chapter"]["id"], chapter_id)

    def test_chat_write_rejects_stale_latest_before_persist(self):
        previous = ChapterRepo(self.db).create(
            self.novel.id, seq=1, title="第一章", content="开头", status="draft",
        )
        request = WriteChapterRequest(directive="续写", previous_chapter_id=previous.id)

        def chunks(_messages, **_options):
            yield "正在生成"
            ChapterRepo(self.db).create(
                self.novel.id, seq=2, title="别处新章", content="已写", status="draft",
            )

        with patch.object(writing.llm, "stream_complete", side_effect=chunks):
            result = events(chat_actions.write_chapter(self.db, self.conv.id, self.message.id, request))
        self.assertEqual(result[-1]["type"], "error")
        self.assertIn("最新章节发生变化", result[-1]["message"])
        self.assertEqual(len(ChapterRepo(self.db).list(self.novel.id)), 2)
        self.assertEqual(self.db.query(models.ChatAction).count(), 0)

    def test_settings_preview_filters_three_types_without_writing(self):
        fake = {
            "text": self.message.content, "targets": [], "matched": [], "reason": "测试",
            "changes": [
                {"type": "character", "action": "create", "data": {"name": "吴迪"}},
                {"type": "location", "action": "create", "data": {"name": "江城"}},
            ],
        }
        with patch.object(inject_idea, "inject_idea", return_value=fake):
            result = chat_actions.preview_settings(self.db, self.conv.id, self.message.id)
        self.assertEqual([ch["type"] for ch in result["changes"]], ["character"])
        self.assertEqual(self.db.query(models.Character).count(), 0)
        self.assertEqual(self.db.query(models.Injection).count(), 0)

    def test_settings_apply_is_atomic_idempotent_and_leaves_one_injection(self):
        suggestions = [{"type": "character", "action": "create", "data": {
            "name": "吴迪", "goal": "调查",
        }}]
        with patch.object(setting_index.embedding, "embed", return_value=[[1.0]]):
            first = chat_actions.apply_settings(self.db, self.conv.id, self.message.id, suggestions)
            second = chat_actions.apply_settings(self.db, self.conv.id, self.message.id, suggestions)
        self.assertEqual(first, second)
        self.assertEqual(self.db.query(models.Character).count(), 1)
        self.assertEqual(self.db.query(models.ChatAction).count(), 1)
        injections = self.db.query(models.Injection).all()
        self.assertEqual(len(injections), 1)
        self.assertEqual(injections[0].channel, "chat")
        self.assertEqual(injections[0].raw_text, self.message.content)

    def test_settings_apply_invalid_suggestion_rolls_back_everything(self):
        suggestions = [
            {"type": "character", "action": "create", "data": {"name": "吴迪"}},
            {"type": "character", "action": "create", "data": {}},
        ]
        with patch.object(setting_index.embedding, "embed", return_value=[[1.0]]):
            with self.assertRaises(Exception):
                chat_actions.apply_settings(self.db, self.conv.id, self.message.id, suggestions)
        self.assertEqual(self.db.query(models.Character).count(), 0)
        self.assertEqual(self.db.query(models.ChatAction).count(), 0)
        self.assertEqual(self.db.query(models.Injection).count(), 0)


if __name__ == "__main__":
    unittest.main()
