"""阶段二单元测试：build_context / settle_chapter / consistency_check。"""
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.application import consistency as consistency_uc
from app.application import context as context_uc
from app.application import settle as settle_uc
from app.infrastructure import models
from app.infrastructure import repository as repos

engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
models.Base.metadata.create_all(bind=engine)
Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)


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
        with patch("app.application.settle.llm.complete", return_value=fake):
            result = settle_uc.settle_chapter(self.db, self.novel_id, chapter.id)

        self.assertEqual(result["summary"], "林越捡到玉佩")
        self.assertEqual(len(result["suggestions"]), 2)
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


if __name__ == "__main__":
    unittest.main()
