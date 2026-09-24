"""方向 B 路由契约：预览只读、确认可重试、全局会话拒绝作品动作。"""
import json
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.application import outline_plan
from app.infrastructure import models
from app.infrastructure.db import get_db
from app.infrastructure.repository import ConversationRepo, NovelRepo
from app.interfaces.api.v1 import conversations, outline


class DirectionBApiTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        models.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, autoflush=False)()
        self.novel = NovelRepo(self.db).create(title="测试作品")
        self.conversation = ConversationRepo(self.db).create(novel_id=self.novel.id)
        self.message = ConversationRepo(self.db).add_message(
            self.conversation.id, "assistant", "第一卷调查神秘古井",
        )
        app = FastAPI()
        app.include_router(conversations.router, prefix="/api/v1")
        app.include_router(outline.router, prefix="/api/v1")
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)
        self.plan = {
            "title": "第一卷", "summary": "调查古井", "acts": [],
            "chapters": [{"title": "第一章", "summary": "古井现身"}],
        }

    def tearDown(self):
        self.client.close()
        self.db.close()
        self.engine.dispose()

    def test_chat_outline_preview_apply_and_get_actions(self):
        path = f"/api/v1/conversations/{self.conversation.id}/messages/{self.message.id}"
        with patch.object(outline_plan.llm, "complete", return_value=json.dumps(self.plan)):
            preview = self.client.post(path + "/outline/preview")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["title"], "第一卷")
        self.assertEqual(self.db.query(models.OutlineNode).count(), 0)

        first = self.client.post(path + "/outline/apply", json={"plan": preview.json()})
        second = self.client.post(path + "/outline/apply", json={"plan": preview.json()})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(self.db.query(models.OutlineNode).count(), 2)
        actions = self.client.get(path + "/actions")
        self.assertEqual(actions.status_code, 200)
        self.assertEqual(actions.json()[0]["action_type"], "outline")

    def test_workbench_and_chat_share_one_apply_shape(self):
        path = f"/api/v1/novels/{self.novel.id}/outline/apply"
        response = self.client.post(path, json={"plan": self.plan})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["node_ids"]), 2)

    def test_global_conversation_and_invalid_plan_are_rejected(self):
        conv = ConversationRepo(self.db).create()
        msg = ConversationRepo(self.db).add_message(conv.id, "assistant", "大纲")
        path = f"/api/v1/conversations/{conv.id}/messages/{msg.id}/outline/apply"
        self.assertEqual(self.client.post(path, json={"plan": self.plan}).status_code, 400)
        invalid = {**self.plan, "chapters": []}
        self.assertEqual(self.client.post(
            f"/api/v1/novels/{self.novel.id}/outline/apply", json={"plan": invalid}
        ).status_code, 422)
        self.assertEqual(self.db.query(models.OutlineNode).count(), 0)


if __name__ == "__main__":
    unittest.main()
