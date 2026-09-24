"""章级大纲契合检查：显式调用、证据校验与零写入。"""

import json
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.application import outline_check
from app.infrastructure import models
from app.infrastructure.db import get_db
from app.infrastructure.repository import ChapterRepo, NovelRepo, OutlineRepo
from app.interfaces.api.v1 import chapters


class OutlineCheckTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        models.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, autoflush=False)()
        self.novel = NovelRepo(self.db).create(title="测试作品")
        volume = OutlineRepo(self.db).create(self.novel.id, title="第一卷", level="volume")
        self.outline = OutlineRepo(self.db).create(
            self.novel.id, title="第二章 再试一次", level="chapter", parent_id=volume.id,
            summary="吴迪落地后闻到消毒水混着铁锈的气味",
        )
        self.chapter = ChapterRepo(self.db).create(
            self.novel.id, seq=2, title="再试一次", content="原有正文", status="draft",
            outline_node_id=self.outline.id,
        )
        app = FastAPI()
        app.include_router(chapters.router, prefix="/api/v1")
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)
        self.path = f"/api/v1/novels/{self.novel.id}/chapters/{self.chapter.id}/outline-check"

    def tearDown(self):
        self.client.close()
        self.db.close()
        self.engine.dispose()

    def test_detects_changed_anchor_without_writing_or_trusting_fake_excerpt(self):
        response_text = json.dumps({"issues": [
            {
                "kind": "changed", "severity": "high",
                "outline_point": "消毒水混着铁锈的气味", "excerpt": "焦糊味",
                "reason": "正文把大纲中的气味换成焦糊味",
            },
            {
                "kind": "missing", "severity": "low",
                "outline_point": "具体线索", "excerpt": "并不存在的引文",
                "reason": "尚未出现",
            },
        ], "truncated": True})
        with patch.object(outline_check.llm, "complete", return_value=response_text) as complete:
            response = self.client.post(self.path, json={"content": "吴迪落地后闻到焦糊味。"})

        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertFalse(data["truncated"])  # 截断标记由服务端决定，不信模型输出。
        self.assertEqual(data["issues"][0]["excerpt"], "焦糊味")
        self.assertEqual(data["issues"][1]["excerpt"], "")
        self.assertIn("消毒水混着铁锈", complete.call_args.args[0][1]["content"])
        self.assertEqual(ChapterRepo(self.db).get(self.novel.id, self.chapter.id).content, "原有正文")
        self.assertEqual(self.db.query(models.ChapterRevision).count(), 0)

    def test_requires_bound_outline_with_summary_before_model_call(self):
        ChapterRepo(self.db).update(self.novel.id, self.chapter.id, outline_node_id="")
        with patch.object(outline_check.llm, "complete") as complete:
            response = self.client.post(self.path, json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("未绑定章级大纲", response.json()["detail"])
        complete.assert_not_called()

    def test_blank_outline_summary_or_content_does_not_call_model(self):
        with patch.object(outline_check.llm, "complete") as complete:
            response = self.client.post(self.path, json={"content": "  "})
            self.assertEqual(response.status_code, 400)
            complete.assert_not_called()

            self.outline.summary = ""
            self.db.commit()
            response = self.client.post(self.path, json={"content": "有正文"})
            self.assertEqual(response.status_code, 400)
            self.assertIn("缺少概要", response.json()["detail"])
            complete.assert_not_called()

    def test_truncated_chapter_keeps_head_and_tail_and_reports_partial_check(self):
        content = "章首线索" + "中段" * 6000 + "结尾线索"
        with patch.object(outline_check.llm, "complete", return_value='{"issues":[]}') as complete:
            response = self.client.post(self.path, json={"content": content})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["truncated"])
        prompt = complete.call_args.args[0][1]["content"]
        self.assertIn("章首线索", prompt)
        self.assertIn("正文中段已省略", prompt)
        self.assertIn("结尾线索", prompt)

    def test_prompt_preserves_repeated_outline_constraints(self):
        self.outline.summary = "吴迪每次瞬移落地后都闻到消毒水混着铁锈的气味"
        self.db.commit()
        with patch.object(outline_check.llm, "complete", return_value='{"issues":[]}') as complete:
            response = self.client.post(self.path, json={"content": "第一次落地。第二次落地，闻到消毒水混着铁锈。"})
        self.assertEqual(response.status_code, 200, response.text)
        messages = complete.call_args.args[0]
        self.assertIn("每次瞬移落地后", messages[1]["content"])
        self.assertIn("正文仅体现一次不能自动视为满足全称要求", messages[0]["content"])

    def test_invalid_model_json_is_not_reported_as_pass(self):
        with patch.object(outline_check.llm, "complete", return_value="没有问题"):
            response = self.client.post(self.path, json={})
        self.assertEqual(response.status_code, 502)
        self.assertIn("格式无效", response.json()["detail"])

    def test_excess_model_issues_are_capped_for_review(self):
        issue = {
            "kind": "missing", "severity": "low", "outline_point": "线索",
            "excerpt": "", "reason": "待核对",
        }
        with patch.object(outline_check.llm, "complete", return_value=json.dumps({"issues": [issue] * 8})):
            response = self.client.post(self.path, json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["issues"]), 6)


if __name__ == "__main__":
    unittest.main()
