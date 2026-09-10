"""阶段六单元测试：全局对话系统（会话 CRUD / 流式 / 上下文 / 转作品 / 级联）。"""
import json
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.application import chat as chat_uc
from app.infrastructure import models
from app.infrastructure import repository as repos

engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
models.Base.metadata.create_all(bind=engine)
Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def _collect(gen) -> list[dict]:
    events = []
    for line in gen:
        line = line.strip()
        if line.startswith("data:"):
            events.append(json.loads(line[5:].strip()))
    return events


class ChatTest(unittest.TestCase):
    def setUp(self):
        self.db = Session()
        self.repo = repos.ConversationRepo(self.db)

    def tearDown(self):
        self.db.close()

    def test_conversation_crud(self):
        novel = repos.NovelRepo(self.db).create(title="作品", genre="玄幻")

        c_global = self.repo.create(novel_id=None)
        self.assertIsNone(c_global.novel_id)

        c_work = self.repo.create(novel_id=novel.id)
        self.assertEqual(c_work.novel_id, novel.id)

        # 重命名
        self.repo.update(c_global.id, title="新标题")
        self.assertEqual(self.repo.get(c_global.id).title, "新标题")

        # 移动（改 novel_id）
        self.repo.update(c_global.id, novel_id=novel.id)
        self.assertEqual(self.repo.get(c_global.id).novel_id, novel.id)

        # 删除级联删消息
        self.repo.add_message(c_work.id, "user", "你好")
        self.repo.delete(c_work.id)
        self.assertEqual(
            self.db.query(models.Message)
            .filter(models.Message.conversation_id == c_work.id)
            .count(),
            0,
        )

    def test_message_streaming(self):
        c = self.repo.create(novel_id=None)
        chunks = ["你好", "，我是灵墨", "。"]
        with patch.object(chat_uc.llm, "stream_complete", return_value=iter(chunks)):
            events = _collect(chat_uc.reply_conversation(self.db, c.id, "你是谁"))

        types = [e["type"] for e in events]
        self.assertIn("token", types)
        done = [e for e in events if e["type"] == "done"][0]

        msgs = self.repo.list_messages(c.id)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0].role, "user")
        self.assertEqual(msgs[1].role, "assistant")
        self.assertEqual(msgs[1].content, "你好，我是灵墨。")
        self.assertEqual(done["user_message_id"], msgs[0].id)
        self.assertEqual(done["assistant_message_id"], msgs[1].id)
        # 标题自动取用户消息前 20 字
        self.assertEqual(self.repo.get(c.id).title, "你是谁")

    def test_build_chat_context(self):
        novel = repos.NovelRepo(self.db).create(title="作品", genre="玄幻")
        nid = novel.id
        repos.CharacterRepo(self.db).create(
            nid, name="林越", role="主角", personality="坚韧", goal="复仇"
        )
        repos.WorldSettingRepo(self.db).create(nid, name="青云宗", category="宗门", description="修仙门派")
        repos.OutlineRepo(self.db).create(nid, title="第一卷", level="volume")
        repos.ChapterRepo(self.db).create(
            nid, seq=1, title="第一章", content="...", status="finalized", summary="林越入门"
        )

        ctx = chat_uc.build_chat_context(self.db, nid)
        self.assertIn("林越", ctx)
        self.assertIn("青云宗", ctx)
        self.assertIn("第一卷", ctx)
        self.assertIn("第1章", ctx)

    def test_promote(self):
        c = self.repo.create(novel_id=None, title="测试会话")
        novel = chat_uc.promote_conversation(self.db, c.id, "测试作品", "玄幻", "简介")
        self.assertEqual(novel.title, "测试作品")
        self.assertEqual(self.repo.get(c.id).novel_id, novel.id)

    def test_novel_cascade_conversations(self):
        novel = repos.NovelRepo(self.db).create(title="作品", genre="玄幻")
        nid = novel.id

        c_work = self.repo.create(novel_id=nid)
        self.repo.add_message(c_work.id, "user", "hi")
        c_global = self.repo.create(novel_id=None)
        self.repo.add_message(c_global.id, "user", "hello")
        work_id = c_work.id
        global_id = c_global.id

        repos.NovelRepo(self.db).delete(nid)

        # 作品会话及其消息清空
        self.assertEqual(
            self.db.query(models.Message)
            .filter(models.Message.conversation_id == work_id)
            .count(),
            0,
        )
        self.assertEqual(
            self.db.query(models.Conversation)
            .filter(models.Conversation.id == work_id)
            .count(),
            0,
        )
        # 全局会话及其消息保留
        self.assertIsNotNone(self.repo.get(global_id))
        self.assertEqual(
            self.db.query(models.Message)
            .filter(models.Message.conversation_id == global_id)
            .count(),
            1,
        )


if __name__ == "__main__":
    unittest.main()
