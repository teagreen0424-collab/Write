"""阶段五单元测试：向量检索 / memory_chunk / injection / build_context 语义召回。"""
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.application import context as context_uc
from app.application import memory
from app.infrastructure import models
from app.infrastructure import repository as repos
from app.infrastructure import vector

engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
models.Base.metadata.create_all(bind=engine)
Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class FakeEmbeddingService:
    """确定性假嵌入，避免依赖真实模型权重。"""

    _map = {
        "林越在青云山捡到玉佩": [1.0, 0.0, 0.0],
        "苏婉儿在城中出场": [0.0, 1.0, 0.0],
    }

    def embed(self, texts: list[str]):
        return [self._map.get(t, [0.0, 0.0, 1.0]) for t in texts]


class MemoryLayerTest(unittest.TestCase):
    def setUp(self):
        self.db = Session()
        novel = repos.NovelRepo(self.db).create(title="测试作品", genre="玄幻", summary="")
        self.nid = novel.id

    def tearDown(self):
        self.db.close()

    def test_vector_store_and_search(self):
        with patch("app.infrastructure.vector.EmbeddingService", return_value=FakeEmbeddingService()):
            vector.store(self.db, self.nid, "林越在青云山捡到玉佩")
            vector.store(self.db, self.nid, "苏婉儿在城中出场")
            results = vector.search(self.db, self.nid, "林越在青云山捡到玉佩", top_k=2)

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["text"], "林越在青云山捡到玉佩")
        self.assertGreater(results[0]["score"], results[1]["score"])

    def test_record_injection(self):
        memory.record_injection(
            self.db,
            self.nid,
            "chat",
            "让林越黑化",
            parsed_intent=[{"type": "character"}],
            applied_changes=[{"type": "character", "action": "update", "id": "x"}],
        )
        injections = repos.InjectionRepo(self.db).list_by_novel(self.nid)
        self.assertEqual(len(injections), 1)
        self.assertEqual(injections[0].channel, "chat")
        self.assertEqual(injections[0].raw_text, "让林越黑化")

    def test_write_chapter_memory(self):
        with patch("app.infrastructure.vector.EmbeddingService", return_value=FakeEmbeddingService()):
            count = memory.write_chapter_memory(self.db, self.nid, "ch1", "林越在青云山捡到玉佩\n\n苏婉儿在城中出场")

        chunks = repos.MemoryChunkRepo(self.db).list_by_novel(self.nid)
        self.assertGreaterEqual(len(chunks), 1)
        self.assertEqual(count, len(chunks))

    def test_build_context_semantic_recall(self):
        repos.ChapterRepo(self.db).create(
            self.nid, seq=1, title="第一章", content="...", status="finalized", summary="林越入门"
        )
        recalled = [{"text": "历史片段：林越曾捡到玉佩", "score": 0.9}]
        with patch("app.infrastructure.vector.search", return_value=recalled):
            text = context_uc.build_context(self.db, self.nid, chapter_position="第 2 章")

        self.assertIn("相关历史片段", text)
        self.assertIn("历史片段：林越曾捡到玉佩", text)

    def test_build_context_recall_degradation(self):
        repos.ChapterRepo(self.db).create(
            self.nid, seq=1, title="第一章", content="...", status="finalized", summary="林越入门"
        )
        with patch("app.infrastructure.vector.search", side_effect=RuntimeError("boom")):
            text = context_uc.build_context(self.db, self.nid, chapter_position="第 2 章")

        self.assertIn("前情摘要链", text)
        self.assertNotIn("相关历史片段", text)


if __name__ == "__main__":
    unittest.main()
