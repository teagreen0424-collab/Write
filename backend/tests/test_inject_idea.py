"""阶段四单元测试：对话式构思注入（意图识别 / 修改现有实体 / 大纲重规划）。"""
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.application import inject_idea as inject_uc
from app.infrastructure import models
from app.infrastructure import repository as repos
from app.interfaces import schemas
from app.interfaces.api.v1 import ideas as ideas_api

engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
models.Base.metadata.create_all(bind=engine)
Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class InjectIdeaTest(unittest.TestCase):
    def setUp(self):
        self.db = Session()
        novel = repos.NovelRepo(self.db).create(title="测试作品", genre="玄幻", summary="")
        self.nid = novel.id

    def tearDown(self):
        self.db.close()

    def test_inject_idea_parse(self):
        fake = (
            '{"targets":["林越"],'
            '"changes":[{"type":"character","action":"update",'
            '"data":{"name":"林越","personality":"黑化","goal":"复仇"}}],'
            '"reason":"因为他知道了身世真相"}'
        )
        with patch.object(inject_uc.llm, "complete", return_value=fake):
            result = inject_uc.inject_idea(self.db, self.nid, "让林越黑化，因为他知道了身世真相")

        self.assertEqual(result["targets"], ["林越"])
        self.assertEqual(result["reason"], "因为他知道了身世真相")
        self.assertEqual(len(result["changes"]), 1)

    def test_preview_repairs_new_character_mislabeled_update_without_name(self):
        fake = (
            '{"targets":["吴迪"],"changes":[{"type":"character","action":"update",'
            '"data":{"goal":"找到工作"}}],"reason":"测试"}'
        )
        with patch.object(inject_uc.llm, "complete", return_value=fake):
            result = inject_uc.inject_idea(self.db, self.nid, "吴迪想找到工作")
        self.assertEqual(result["changes"][0]["action"], "create")
        self.assertEqual(result["changes"][0]["data"]["name"], "吴迪")
        self.assertEqual(repos.CharacterRepo(self.db).list(self.nid), [])

    def test_preview_keeps_existing_character_update(self):
        repos.CharacterRepo(self.db).create(self.nid, name="吴迪", goal="活下去")
        fake = (
            '{"targets":["吴迪"],"changes":[{"type":"character","action":"update",'
            '"data":{"goal":"找到工作"}}],"reason":"测试"}'
        )
        with patch.object(inject_uc.llm, "complete", return_value=fake):
            result = inject_uc.inject_idea(self.db, self.nid, "吴迪想找到工作")
        self.assertEqual(result["changes"][0]["action"], "update")
        self.assertEqual(result["changes"][0]["data"]["name"], "吴迪")

    def test_preview_normalizes_invalid_foreshadowing_status(self):
        fake = (
            '{"targets":[],"changes":[{"type":"foreshadowing","action":"create",'
            '"data":{"content":"瞬移后的陌生气味","status":"已埋下（第一章）"}}],'
            '"reason":"测试"}'
        )
        with patch.object(inject_uc.llm, "complete", return_value=fake):
            result = inject_uc.inject_idea(self.db, self.nid, "设计伏笔")
        self.assertEqual(result["changes"][0]["data"]["status"], "planted")

    def test_apply_update_existing_entity(self):
        repos.CharacterRepo(self.db).create(self.nid, name="林越", personality="温和")
        changes = [
            {
                "type": "character",
                "action": "update",
                "data": {"name": "林越", "personality": "黑化", "goal": "复仇"},
            }
        ]
        result = inject_uc.apply_injections(self.db, self.nid, changes)

        chars = repos.CharacterRepo(self.db).list(self.nid)
        self.assertEqual(len(chars), 1)  # 未新建同名人物
        self.assertEqual(chars[0].personality, "黑化")
        self.assertEqual(chars[0].goal, "复仇")
        self.assertEqual(result["applied"][0]["action"], "update")

    def test_apply_triggers_outline_update(self):
        repos.CharacterRepo(self.db).create(self.nid, name="林越", personality="温和")
        vol = repos.OutlineRepo(self.db).create(self.nid, title="第一卷", level="volume")
        ch = repos.OutlineRepo(self.db).create(
            self.nid, title="第一章", level="chapter", parent_id=vol.id, summary="旧概要"
        )
        changes = [
            {
                "type": "character",
                "action": "update",
                "data": {"name": "林越", "personality": "黑化"},
            }
        ]
        replan = '{"outline_updates":[{"title":"第一章","summary":"林越黑化后的新概要"}]}'
        with patch.object(inject_uc.llm, "complete", return_value=replan):
            result = inject_uc.apply_injections(self.db, self.nid, changes)

        node = repos.OutlineRepo(self.db).get(self.nid, ch.id)
        self.assertEqual(node.summary, "林越黑化后的新概要")
        self.assertEqual(len(result["outline_updates"]), 1)

    def test_normalize_timeline_event_name(self):
        changes = [
            {
                "type": "timeline_event",
                "action": "create",
                "data": {"name": "林越得知身世真相", "time_point": "第3章"},
            }
        ]
        result = inject_uc.apply_injections(self.db, self.nid, changes)

        events = repos.TimelineEventRepo(self.db).list(self.nid)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].title, "林越得知身世真相")
        self.assertEqual(result["applied"][0]["action"], "create")

    def test_normalize_foreshadowing_name(self):
        changes = [
            {
                "type": "foreshadowing",
                "action": "create",
                "data": {"name": "林越的身世之谜", "status": "planted"},
            }
        ]
        result = inject_uc.apply_injections(self.db, self.nid, changes)

        foreshadowings = repos.ForeshadowingRepo(self.db).list(self.nid)
        self.assertEqual(len(foreshadowings), 1)
        self.assertEqual(foreshadowings[0].content, "林越的身世之谜")
        self.assertEqual(result["applied"][0]["action"], "create")

    def test_apply_route_records_one_chat_injection_with_raw_text(self):
        raw_text = "让林越改为以复仇为目标。"
        payload = schemas.IdeaApplyRequest(
            suggestions=[
                {
                    "type": "character",
                    "action": "create",
                    "data": {"name": "林越", "goal": "复仇"},
                }
            ],
            raw_text=raw_text,
            channel="chat",
        )

        result = ideas_api.apply(self.nid, payload, self.db)

        chars = repos.CharacterRepo(self.db).list(self.nid)
        injections = repos.InjectionRepo(self.db).list_by_novel(self.nid)
        self.assertEqual(len(result["applied"]), 1)
        self.assertEqual(chars[0].goal, "复仇")
        self.assertEqual(len(injections), 1)
        self.assertEqual(injections[0].channel, "chat")
        self.assertEqual(injections[0].raw_text, raw_text)

    def test_apply_route_empty_raw_text_keeps_legacy_behavior(self):
        payload = schemas.IdeaApplyRequest(
            suggestions=[
                {
                    "type": "location",
                    "action": "create",
                    "data": {"name": "青云山"},
                }
            ]
        )

        result = ideas_api.apply(self.nid, payload, self.db)

        locations = repos.LocationRepo(self.db).list(self.nid)
        injections = repos.InjectionRepo(self.db).list_by_novel(self.nid)
        self.assertEqual(len(result["applied"]), 1)
        self.assertEqual(locations[0].name, "青云山")
        self.assertEqual(injections, [])


if __name__ == "__main__":
    unittest.main()
