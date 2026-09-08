"""阶段四单元测试：对话式构思注入（意图识别 / 修改现有实体 / 大纲重规划）。"""
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.application import inject_idea as inject_uc
from app.infrastructure import models
from app.infrastructure import repository as repos

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


if __name__ == "__main__":
    unittest.main()
