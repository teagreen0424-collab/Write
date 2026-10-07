"""作者建议确认：引用解析、逐条失败反馈和原子聊天事务。"""
import json
import unittest
import uuid
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.application import chat_actions, inject_idea, reference_resolution, setting_index, settle
from app.infrastructure import models, repository as repos


class ReferenceResolutionTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        models.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, autoflush=False)()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        mocked = patch.object(setting_index.embedding, "embed", return_value=[[1.0]])
        mocked.start()
        self.addCleanup(mocked.stop)
        self.nid = repos.NovelRepo(self.db).create(title="本作品").id
        self.other = repos.NovelRepo(self.db).create(title="另一作品").id
        self.chapter = repos.ChapterRepo(self.db).create(self.nid, title="第一章")
        self.person = repos.CharacterRepo(self.db).create(self.nid, name="林越")

    def test_preview_resolves_existing_names_without_writing(self):
        place = repos.LocationRepo(self.db).create(self.nid, name="青云城")
        changes = [
            {"type": "timeline_event", "data": {"title": "入城", "participants": ["林越"]}},
            {"type": "foreshadowing", "data": {"content": "玉佩", "related_characters": ["林越"]}},
            {"type": "relationship", "data": {"subject_id": "林越", "object_id": "青云城", "relation_type": "居住"}},
        ]
        with patch.object(inject_idea.llm, "complete", return_value=json.dumps({"changes": changes})):
            result = inject_idea.inject_idea(self.db, self.nid, "林越入青云城")
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["changes"][0]["data"]["participants"], [self.person.id])
        self.assertEqual(result["changes"][1]["data"]["related_characters"], [self.person.id])
        self.assertEqual(result["changes"][2]["data"]["object_id"], place.id)
        self.assertEqual(self.db.query(models.TimelineEvent).count(), 0)
        self.assertEqual(self.db.query(models.Injection).count(), 0)

    def test_preview_keeps_unknown_and_ambiguous_references_with_explicit_errors(self):
        repos.CharacterRepo(self.db).create(self.nid, name="林越")
        changes = [
            {"type": "timeline_event", "data": {"title": "入城", "participants": ["林越"]}},
            {"type": "foreshadowing", "data": {"content": "玉佩", "related_characters": ["未知人物"]}},
        ]
        with patch.object(inject_idea.llm, "complete", return_value=json.dumps({"changes": changes})):
            result = inject_idea.inject_idea(self.db, self.nid, "构思")
        self.assertEqual([item["index"] for item in result["failed"]], [0, 1])
        self.assertIn("同名", result["failed"][0]["error"])
        self.assertIn("不存在", result["failed"][1]["error"])
        self.assertEqual(result["changes"][0]["data"]["participants"], ["林越"])
        self.assertEqual(self.db.query(models.Character).count(), 2)

    def test_legacy_apply_resolves_prior_same_batch_created_character(self):
        result = inject_idea.apply_injections(self.db, self.nid, [
            {"type": "character", "data": {"name": "新人物"}},
            {"type": "timeline_event", "data": {"title": "新事件", "participants": ["新人物", self.person.id]}},
            {"type": "foreshadowing", "data": {"content": "新伏笔", "related_characters": ["新人物"], "planted_chapter_id": "第一章"}},
        ], raw_text="作者构思")
        self.assertEqual(result["failed"], [])
        self.assertEqual([item["index"] for item in result["applied"]], [0, 1, 2])
        created_id = result["applied"][0]["id"]
        self.assertEqual(self.db.query(models.TimelineEvent).one().participants, [created_id, self.person.id])
        self.assertEqual(self.db.query(models.Foreshadowing).one().related_characters, [created_id])
        self.assertEqual(self.db.query(models.Foreshadowing).one().planted_chapter_id, self.chapter.id)

    def test_settlement_resolves_existing_and_prior_same_batch_names(self):
        result = settle.apply_suggestions(self.db, self.nid, [
            {"type": "character", "data": {"name": "新人物"}},
            {"type": "timeline_event", "data": {"title": "新事件", "participants": ["林越", "新人物"]}},
            {"type": "foreshadowing", "data": {"content": "新伏笔", "related_characters": [self.person.id]}},
        ], self.chapter.id)
        self.assertEqual(result["failed"], [])
        self.assertEqual(len(result["created"]), 3)
        self.assertEqual(self.db.query(models.TimelineEvent).one().participants,
                         [self.person.id, result["created"][0]["id"]])
        self.assertEqual(self.db.query(models.Foreshadowing).one().planted_chapter_id, self.chapter.id)

    def test_all_failed_injection_reports_failure_without_success_history(self):
        foreign = repos.CharacterRepo(self.db).create(self.other, name="外部人物")
        result = inject_idea.apply_injections(self.db, self.nid, [
            {"type": "timeline_event", "data": {"title": "坏事件", "participants": [foreign.id]}},
        ], raw_text="不能假装落地")
        self.assertEqual(result["applied"], [])
        self.assertEqual(result["failed"][0]["index"], 0)
        self.assertIn("其他作品", result["failed"][0]["error"])
        self.assertEqual(self.db.query(models.TimelineEvent).count(), 0)
        self.assertEqual(self.db.query(models.Injection).count(), 0)

    def test_all_failed_settlement_retains_specific_reference_error(self):
        result = settle.apply_suggestions(self.db, self.nid, [
            {"type": "foreshadowing", "data": {"content": "谜", "related_characters": ["未知人物"]}},
        ], self.chapter.id)
        self.assertEqual(result["created"], [])
        self.assertEqual(result["failed"][0]["type"], "foreshadowing")
        self.assertIn("未知人物", result["failed"][0]["error"])
        self.assertEqual(self.db.query(models.Foreshadowing).count(), 0)

    def test_legacy_partial_success_keeps_success_and_reports_failed_index(self):
        result = inject_idea.apply_injections(self.db, self.nid, [
            {"type": "location", "data": {"name": "已保存地点"}},
            {"type": "timeline_event", "data": {"title": "未保存事件", "participants": ["未知人物"]}},
        ], raw_text="部分成功")
        self.assertEqual(result["applied"][0]["index"], 0)
        self.assertEqual(result["failed"][0]["index"], 1)
        self.assertEqual(self.db.query(models.Location).count(), 1)
        self.assertEqual(self.db.query(models.TimelineEvent).count(), 0)
        self.assertEqual(self.db.query(models.Injection).one().applied_changes, result["applied"])

    def test_settlement_partial_retry_only_failed_item_does_not_duplicate_success(self):
        first = settle.apply_suggestions(self.db, self.nid, [
            {"type": "timeline_event", "data": {"title": "待修正", "participants": ["未知人物"]}},
            {"type": "character", "data": {"name": "已创建人物"}},
        ], self.chapter.id)
        self.assertEqual(first["created"][0]["index"], 1)
        self.assertEqual(first["failed"][0]["index"], 0)
        retried = settle.apply_suggestions(self.db, self.nid, [
            {"type": "timeline_event", "data": {"title": "待修正", "participants": ["已创建人物"]}},
        ], self.chapter.id)
        self.assertEqual(retried["failed"], [])
        self.assertEqual(self.db.query(models.Character).count(), 2)
        self.assertEqual(self.db.query(models.TimelineEvent).one().participants, [first["created"][0]["id"]])

    def test_foreign_same_name_does_not_ambiguate_own_unique_name(self):
        repos.CharacterRepo(self.db).create(self.other, name="林越")
        result = reference_resolution.resolve_references(self.db, self.nid, "timeline_event", {"participants": ["林越"]})
        self.assertEqual(result["participants"], [self.person.id])

    def test_duplicate_names_never_choose_first_record(self):
        repos.CharacterRepo(self.db).create(self.nid, name="林越")
        result = inject_idea.apply_injections(self.db, self.nid, [
            {"type": "timeline_event", "data": {"title": "待定", "participants": ["林越"]}},
        ])
        self.assertEqual(result["applied"], [])
        self.assertIn("多个同名", result["failed"][0]["error"])
        self.assertEqual(reference_resolution.resolve_references(self.db, self.nid, "timeline_event",
                         {"participants": [self.person.id]})["participants"], [self.person.id])

    def test_relation_name_collision_across_node_types_requires_explicit_id(self):
        place = repos.LocationRepo(self.db).create(self.nid, name="林越")
        with self.assertRaises(HTTPException) as error:
            reference_resolution.resolve_references(self.db, self.nid, "relationship",
                                                   {"subject_id": "林越", "object_id": place.id})
        self.assertEqual(error.exception.status_code, 400)
        self.assertIn("多个同名", error.exception.detail)

    def test_unknown_uuid_is_not_guessed_as_a_character_name(self):
        missing_id = str(uuid.uuid4())
        repos.CharacterRepo(self.db).create(self.nid, name=missing_id)
        with self.assertRaises(HTTPException) as error:
            reference_resolution.resolve_references(self.db, self.nid, "timeline_event", {"participants": [missing_id]})
        self.assertIn("ID", error.exception.detail)

    def test_update_matches_declared_kind_when_character_and_world_share_name(self):
        world = repos.WorldSettingRepo(self.db).create(self.nid, name="林越", description="旧世界观")
        result = inject_idea.apply_injections(self.db, self.nid, [
            {"type": "world_setting", "action": "update", "data": {"name": "林越", "description": "新世界观"}},
        ])
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["applied"][0]["id"], world.id)
        self.assertEqual(result["applied"][0]["action"], "update")
        self.assertEqual(self.db.query(models.WorldSetting).count(), 1)
        self.assertEqual(repos.WorldSettingRepo(self.db).get(self.nid, world.id).description, "新世界观")
        self.assertEqual(repos.CharacterRepo(self.db).get(self.nid, self.person.id).goal, "")

    def test_ambiguous_primary_update_modifies_nothing_and_preview_keeps_failure(self):
        repos.CharacterRepo(self.db).create(self.nid, name="林越", goal="另一个目标")
        change = {"type": "character", "action": "update", "data": {"name": "林越", "goal": "不应写入"}}
        before = [(item.id, item.goal) for item in self.db.query(models.Character).order_by(models.Character.id).all()]
        result = inject_idea.apply_injections(self.db, self.nid, [change], raw_text="同名构思")
        self.assertEqual(result["applied"], [])
        self.assertIn("同名", result["failed"][0]["error"])
        self.assertEqual([(item.id, item.goal) for item in self.db.query(models.Character).order_by(models.Character.id).all()], before)
        self.assertEqual(self.db.query(models.Injection).count(), 0)
        with patch.object(inject_idea.llm, "complete", return_value=json.dumps({"targets": ["林越"], "changes": [change]})):
            preview = inject_idea.inject_idea(self.db, self.nid, "同名构思")
        self.assertTrue(preview["matched"][0]["ambiguous"])
        self.assertEqual(preview["changes"][0]["action"], "update")
        self.assertIn("同名", preview["failed"][0]["error"])

    def test_ambiguous_primary_update_keeps_legacy_partial_success(self):
        repos.CharacterRepo(self.db).create(self.nid, name="林越")
        result = inject_idea.apply_injections(self.db, self.nid, [
            {"type": "location", "data": {"name": "已保存地点"}},
            {"type": "character", "action": "update", "data": {"name": "林越", "goal": "不应写入"}},
        ])
        self.assertEqual(result["applied"][0]["index"], 0)
        self.assertEqual(result["failed"][0]["index"], 1)
        self.assertEqual(self.db.query(models.Location).count(), 1)
        self.assertTrue(all(not item.goal for item in self.db.query(models.Character).all()))

    def test_ambiguous_primary_update_rolls_back_prior_atomic_create(self):
        repos.CharacterRepo(self.db).create(self.nid, name="林越")
        with self.assertRaises(HTTPException):
            inject_idea.apply_injections(self.db, self.nid, [
                {"type": "character", "data": {"name": "不应保留"}},
                {"type": "character", "action": "update", "data": {"name": "林越", "goal": "不应写入"}},
            ], atomic=True)
        self.assertEqual(self.db.query(models.Character).count(), 2)
        self.assertTrue(all(not item.goal for item in self.db.query(models.Character).all()))

    def test_missing_primary_update_keeps_existing_create_fallback(self):
        result = inject_idea.apply_injections(self.db, self.nid, [
            {"type": "world_setting", "action": "update", "data": {"name": "不存在的世界观", "description": "新增"}},
        ])
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["applied"][0]["action"], "create")
        self.assertEqual(self.db.query(models.WorldSetting).one().name, "不存在的世界观")

    def test_atomic_confirmation_failure_rolls_back_entities_action_and_history(self):
        conversation = repos.ConversationRepo(self.db).create(novel_id=self.nid)
        message = repos.ConversationRepo(self.db).add_message(conversation.id, "assistant", "构思")
        suggestions = [
            {"type": "character", "data": {"name": "不应保留"}},
            {"type": "foreshadowing", "data": {"content": "谜", "related_characters": ["未知人物"]}},
        ]
        with self.assertRaises(HTTPException):
            chat_actions.apply_settings(self.db, conversation.id, message.id, suggestions)
        self.assertEqual(self.db.query(models.Character).count(), 1)
        self.assertEqual(self.db.query(models.ChatAction).count(), 0)
        self.assertEqual(self.db.query(models.Injection).count(), 0)

    def test_atomic_confirmation_can_resolve_same_batch_uncommitted_character(self):
        conversation = repos.ConversationRepo(self.db).create(novel_id=self.nid)
        message = repos.ConversationRepo(self.db).add_message(conversation.id, "assistant", "构思")
        result = chat_actions.apply_settings(self.db, conversation.id, message.id, [
            {"type": "character", "data": {"name": "新增"}},
            {"type": "foreshadowing", "data": {"content": "谜", "related_characters": ["新增"]}},
        ])
        self.assertEqual(self.db.query(models.Foreshadowing).one().related_characters, [result["applied"][0]["id"]])
        self.assertEqual(self.db.query(models.Injection).count(), 1)

    def test_legacy_replan_failure_reports_saved_entities_without_retrying_them(self):
        repos.OutlineRepo(self.db).create(self.nid, title="后续章", level="chapter", summary="原概要")
        with patch.object(inject_idea.llm, "complete", side_effect=RuntimeError("模型不可用")):
            result = inject_idea.apply_injections(self.db, self.nid, [
                {"type": "character", "action": "update", "data": {"name": "林越", "goal": "新目标"}},
            ], raw_text="构思")
        self.assertEqual(len(result["applied"]), 1)
        self.assertEqual(result["failed"], [])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertEqual(repos.CharacterRepo(self.db).get(self.nid, self.person.id).goal, "新目标")
        self.assertEqual(self.db.query(models.OutlineNode).one().summary, "原概要")

    def test_injection_history_failure_reports_saved_entities_without_fake_retry(self):
        original_commit = self.db.commit

        def commit():
            if any(isinstance(item, models.Injection) for item in self.db.new):
                raise RuntimeError("留痕写库失败")
            return original_commit()

        with patch.object(self.db, "commit", side_effect=commit):
            result = inject_idea.apply_injections(self.db, self.nid, [
                {"type": "location", "data": {"name": "已保存地点"}},
            ], raw_text="作者构思")
        self.assertEqual(len(result["applied"]), 1)
        self.assertEqual(result["failed"], [])
        self.assertIn("留痕失败", result["warnings"][0])
        self.assertEqual(self.db.query(models.Location).count(), 1)
        self.assertEqual(self.db.query(models.Injection).count(), 0)

    def test_atomic_injection_history_failure_rolls_back_every_entity_and_action(self):
        conversation = repos.ConversationRepo(self.db).create(novel_id=self.nid)
        message = repos.ConversationRepo(self.db).add_message(conversation.id, "assistant", "构思")
        original_flush = self.db.flush

        def flush(*args, **kwargs):
            if any(isinstance(item, models.Injection) for item in self.db.new):
                raise RuntimeError("留痕写库失败")
            return original_flush(*args, **kwargs)

        with patch.object(self.db, "flush", side_effect=flush):
            with self.assertRaises(RuntimeError):
                chat_actions.apply_settings(self.db, conversation.id, message.id, [
                    {"type": "character", "data": {"name": "不应保留"}},
                ])
        self.assertEqual(self.db.query(models.Character).count(), 1)
        self.assertEqual(self.db.query(models.ChatAction).count(), 0)
        self.assertEqual(self.db.query(models.Injection).count(), 0)

    def test_legacy_consistency_read_failure_reports_saved_entities_without_fake_retry(self):
        with patch.object(repos.ForeshadowingRepo, "list", side_effect=RuntimeError("一致性读库失败")):
            result = inject_idea.apply_injections(self.db, self.nid, [
                {"type": "location", "data": {"name": "已保存地点"}},
            ], raw_text="作者构思")
        self.assertEqual(len(result["applied"]), 1)
        self.assertEqual(result["applied"][0]["index"], 0)
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["consistency"]["status"], "failed")
        self.assertIn("一致性检查失败", result["warnings"][0])
        self.assertEqual(self.db.query(models.Location).count(), 1)
        self.assertEqual(self.db.query(models.Injection).count(), 1)

    def test_atomic_consistency_read_failure_rolls_back_entities_and_action(self):
        conversation = repos.ConversationRepo(self.db).create(novel_id=self.nid)
        message = repos.ConversationRepo(self.db).add_message(conversation.id, "assistant", "构思")
        with patch.object(repos.ForeshadowingRepo, "list", side_effect=RuntimeError("一致性读库失败")):
            with self.assertRaises(RuntimeError):
                chat_actions.apply_settings(self.db, conversation.id, message.id, [
                    {"type": "character", "data": {"name": "不应保留"}},
                ])
        self.assertEqual(self.db.query(models.Character).count(), 1)
        self.assertEqual(self.db.query(models.ChatAction).count(), 0)
        self.assertEqual(self.db.query(models.Injection).count(), 0)

    def test_all_failed_injection_does_not_claim_saved_checks_or_run_post_write_work(self):
        with patch.object(inject_idea, "consistency_check", side_effect=RuntimeError("不应运行")) as check:
            result = inject_idea.apply_injections(self.db, self.nid, [
                {"type": "timeline_event", "data": {"title": "坏事件", "participants": ["未知人物"]}},
            ], raw_text="作者构思")
        check.assert_not_called()
        self.assertEqual(result["applied"], [])
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["warnings"], [])
        self.assertEqual(self.db.query(models.Injection).count(), 0)

    def same_title_outline(self):
        first_volume = repos.OutlineRepo(self.db).create(self.nid, title="第一卷", level="volume")
        second_volume = repos.OutlineRepo(self.db).create(self.nid, title="第二卷", level="volume")
        first = repos.OutlineRepo(self.db).create(self.nid, title="开端", level="chapter", parent_id=first_volume.id, summary="原甲")
        second = repos.OutlineRepo(self.db).create(self.nid, title="开端", level="chapter", parent_id=second_volume.id, summary="原乙")
        return first.id, second.id

    def test_legacy_replan_rejects_cross_volume_duplicate_title_without_guessing(self):
        first, second = self.same_title_outline()
        model_result = json.dumps({"outline_updates": [{"title": "开端", "summary": "不应写入"}]})
        with patch.object(inject_idea.llm, "complete", return_value=model_result):
            result = inject_idea.apply_injections(self.db, self.nid, [
                {"type": "character", "action": "update", "data": {"name": "林越", "goal": "已保存目标"}},
            ], raw_text="作者构思")
        self.assertEqual(result["outline_updates"], [])
        self.assertEqual(result["failed"], [])
        self.assertEqual(len(result["applied"]), 1)
        self.assertEqual(len(result["warnings"]), 1)
        self.assertEqual(repos.OutlineRepo(self.db).get(self.nid, first).summary, "原甲")
        self.assertEqual(repos.OutlineRepo(self.db).get(self.nid, second).summary, "原乙")
        self.assertEqual(repos.CharacterRepo(self.db).get(self.nid, self.person.id).goal, "已保存目标")

    def test_atomic_replan_duplicate_title_rolls_back_settings_and_action(self):
        first, second = self.same_title_outline()
        conversation = repos.ConversationRepo(self.db).create(novel_id=self.nid)
        message = repos.ConversationRepo(self.db).add_message(conversation.id, "assistant", "构思")
        model_result = json.dumps({"outline_updates": [{"title": "开端", "summary": "不应写入"}]})
        with patch.object(inject_idea.llm, "complete", return_value=model_result):
            with self.assertRaises(HTTPException) as error:
                chat_actions.apply_settings(self.db, conversation.id, message.id, [
                    {"type": "character", "action": "update", "data": {"name": "林越", "goal": "不应保存"}},
                ])
        self.assertIn("同名章节", error.exception.detail)
        self.assertEqual(repos.CharacterRepo(self.db).get(self.nid, self.person.id).goal, "")
        self.assertEqual(self.db.query(models.ChatAction).count(), 0)
        self.assertEqual(self.db.query(models.Injection).count(), 0)
        self.assertEqual(repos.OutlineRepo(self.db).get(self.nid, first).summary, "原甲")
        self.assertEqual(repos.OutlineRepo(self.db).get(self.nid, second).summary, "原乙")

    def test_settlement_rejects_foreign_source_chapter_before_creating_any_entity(self):
        foreign_chapter = repos.ChapterRepo(self.db).create(self.other, title="异章")
        with self.assertRaises(HTTPException) as error:
            settle.apply_suggestions(self.db, self.nid, [{"type": "character", "data": {"name": "不应创建"}}], foreign_chapter.id)
        self.assertEqual(error.exception.status_code, 404)
        self.assertEqual(self.db.query(models.Character).count(), 1)

    def test_preview_filter_reindexes_failures_for_settings_card(self):
        conversation = repos.ConversationRepo(self.db).create(novel_id=self.nid)
        message = repos.ConversationRepo(self.db).add_message(conversation.id, "assistant", "构思")
        fake = {"changes": [{"type": "timeline_event", "data": {}}, {"type": "foreshadowing", "data": {}}],
                "failed": [{"index": 1, "type": "foreshadowing", "error": "未知引用"}]}
        with patch.object(inject_idea, "inject_idea", return_value=fake):
            result = chat_actions.preview_settings(self.db, conversation.id, message.id)
        self.assertEqual(result["failed"][0]["index"], 0)
