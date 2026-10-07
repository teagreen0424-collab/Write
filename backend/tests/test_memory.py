"""阶段五单元测试：向量检索 / memory_chunk / injection / build_context 语义召回。"""
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.application import context as context_uc
from app.application import memory
from app.application import settle
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
        chapter = repos.ChapterRepo(self.db).create(self.nid, title="第一章")
        with patch("app.infrastructure.vector.EmbeddingService", return_value=FakeEmbeddingService()):
            count = memory.write_chapter_memory(self.db, self.nid, chapter.id, "林越在青云山捡到玉佩\n\n苏婉儿在城中出场")

        chunks = repos.MemoryChunkRepo(self.db).list_by_novel(self.nid)
        self.assertGreaterEqual(len(chunks), 1)
        self.assertEqual(count.written, len(chunks))
        self.assertEqual(count.status, "saved")

    def test_repeated_settlement_replaces_old_memory_and_search_never_recalls_old_draft(self):
        chapter = repos.ChapterRepo(self.db).create(self.nid, title="第一章", content="林越仍在北城。")
        with (
            patch("app.infrastructure.vector.EmbeddingService", return_value=FakeEmbeddingService()),
            patch.object(settle.llm, "complete", return_value='{"summary":"本章摘要"}'),
        ):
            settle.settle_chapter(self.db, self.nid, chapter.id)
            settle.settle_chapter(self.db, self.nid, chapter.id)
            chunks = repos.MemoryChunkRepo(self.db).list_by_novel(self.nid)
            self.assertEqual([item.text for item in chunks], ["林越仍在北城。"])
            repos.ChapterRepo(self.db).update(self.nid, chapter.id, content="林越已经离开北城。")
            settle.settle_chapter(self.db, self.nid, chapter.id)
            recalled = vector.search(self.db, self.nid, "北城", top_k=10)
        self.assertEqual([item["text"] for item in recalled], ["林越已经离开北城。"])

    def test_failed_embedding_invalidates_old_memory_and_keeps_other_chapter_and_settings(self):
        chapter = repos.ChapterRepo(self.db).create(self.nid, title="待替换章")
        other = repos.ChapterRepo(self.db).create(self.nid, title="保留章")
        for kind, source, text in (("chapter_slice", chapter.id, "旧正文"),
                                   ("chapter_slice", other.id, "另一章正文"),
                                   ("setting", "", "人物设定")):
            repos.MemoryChunkRepo(self.db).create(self.nid, kind=kind, source_chapter_id=source, text=text, vector=[1.0])
        with patch("app.infrastructure.vector.EmbeddingService") as service:
            service.return_value.embed.side_effect = RuntimeError("离线")
            count = memory.write_chapter_memory(self.db, self.nid, chapter.id, "新正文")
        self.assertEqual(count.written, 0)
        self.assertEqual(count.status, "embedding_failed")
        self.assertTrue(count.warnings)
        self.assertEqual({item.text for item in repos.MemoryChunkRepo(self.db).list_by_novel(self.nid)}, {"另一章正文", "人物设定"})

    def test_partial_embedding_failure_writes_only_new_successful_slices(self):
        chapter = repos.ChapterRepo(self.db).create(self.nid, title="待替换章")
        repos.MemoryChunkRepo(self.db).create(self.nid, kind="chapter_slice", source_chapter_id=chapter.id, text="旧正文", vector=[1.0])
        with patch("app.infrastructure.vector.EmbeddingService") as service:
            service.return_value.embed.side_effect = [RuntimeError("一块失败"), [[1.0]]]
            count = memory.write_chapter_memory(self.db, self.nid, chapter.id, "甲" * 400 + "\n" + "乙" * 400)
        self.assertEqual(count.written, 1)
        self.assertEqual((count.total, count.failed, count.status), (2, 1, "partial"))
        self.assertEqual([item.text for item in repos.MemoryChunkRepo(self.db).list_by_novel(self.nid)], ["乙" * 400])

    def test_cross_novel_memory_replace_returns_404_without_touching_old_memory(self):
        chapter = repos.ChapterRepo(self.db).create(self.nid, title="第一章")
        repos.MemoryChunkRepo(self.db).create(self.nid, kind="chapter_slice", source_chapter_id=chapter.id, text="原记忆", vector=[1.0])
        other_novel = repos.NovelRepo(self.db).create(title="另一作品")
        with patch("app.infrastructure.vector.EmbeddingService") as service:
            with self.assertRaises(HTTPException) as failure:
                memory.write_chapter_memory(self.db, other_novel.id, chapter.id, "异作品正文")
            service.assert_not_called()
        self.assertEqual(failure.exception.status_code, 404)
        self.assertEqual([item.text for item in repos.MemoryChunkRepo(self.db).list_by_novel(self.nid)], ["原记忆"])
        self.assertEqual(repos.MemoryChunkRepo(self.db).list_by_novel(other_novel.id), [])

    def test_database_failure_rolls_back_entire_memory_replacement(self):
        chapter = repos.ChapterRepo(self.db).create(self.nid, title="第一章", content="甲" * 400 + "\n" + "乙" * 400)
        old = repos.MemoryChunkRepo(self.db).create(self.nid, kind="chapter_slice", source_chapter_id=chapter.id, text="原记忆", vector=[1.0])
        old_id = old.id
        with (
            patch("app.infrastructure.vector.EmbeddingService", return_value=FakeEmbeddingService()),
            patch.object(self.db, "commit", side_effect=RuntimeError("写库失败")),
        ):
            count = memory.write_chapter_memory(self.db, self.nid, chapter.id, "甲" * 400 + "\n" + "乙" * 400)
        self.assertEqual(count.written, 0)
        self.assertEqual(count.status, "storage_failed")
        self.assertIn("清理失败", count.warnings[0])
        stored = repos.MemoryChunkRepo(self.db).list_by_novel(self.nid)
        self.assertEqual([(item.id, item.text) for item in stored], [(old_id, "原记忆")])
        with patch("app.infrastructure.vector.EmbeddingService", return_value=FakeEmbeddingService()):
            self.assertEqual(vector.search(self.db, self.nid, "原记忆"), [])

    def test_empty_current_chapter_removes_previous_slices(self):
        chapter = repos.ChapterRepo(self.db).create(self.nid, title="空章")
        repos.MemoryChunkRepo(self.db).create(self.nid, kind="chapter_slice", source_chapter_id=chapter.id, text="旧正文", vector=[1.0])
        with patch("app.infrastructure.vector.EmbeddingService", return_value=FakeEmbeddingService()):
            count = memory.write_chapter_memory(self.db, self.nid, chapter.id, "")
        self.assertEqual(count.written, 0)
        self.assertEqual(count.status, "empty")
        self.assertEqual(count.warnings, [])
        self.assertEqual(repos.MemoryChunkRepo(self.db).list_by_novel(self.nid), [])

    def test_failed_replacement_cleanup_invalidates_only_target_chapter(self):
        chapter = repos.ChapterRepo(self.db).create(self.nid, title="目标章", content="新正文")
        other = repos.ChapterRepo(self.db).create(self.nid, title="保留章", content="保留正文")
        other_novel = repos.NovelRepo(self.db).create(title="另一作品")
        foreign = repos.ChapterRepo(self.db).create(other_novel.id, title="异作品章", content="异作品正文")
        for novel_id, kind, source, text in (
            (self.nid, "chapter_slice", chapter.id, "旧正文"),
            (self.nid, "chapter_slice", other.id, "保留正文"),
            (self.nid, "setting", "", "人物设定"),
            (other_novel.id, "chapter_slice", foreign.id, "异作品正文"),
        ):
            repos.MemoryChunkRepo(self.db).create(novel_id, kind=kind, source_chapter_id=source, text=text, vector=[1.0])
        with (
            patch("app.infrastructure.vector.EmbeddingService", return_value=FakeEmbeddingService()),
            patch.object(self.db, "add_all", side_effect=RuntimeError("新记忆写入失败")),
        ):
            result = memory.write_chapter_memory(self.db, self.nid, chapter.id, chapter.content)
        self.assertEqual(result.status, "storage_failed")
        self.assertIn("旧记忆已失效", result.warnings[0])
        self.assertEqual({c.text for c in repos.MemoryChunkRepo(self.db).list_by_novel(self.nid)}, {"保留正文", "人物设定"})
        self.assertEqual([c.text for c in repos.MemoryChunkRepo(self.db).list_by_novel(other_novel.id)], ["异作品正文"])

    def test_embedding_initialization_failure_is_reported_and_invalidates_old_draft(self):
        chapter = repos.ChapterRepo(self.db).create(self.nid, title="第一章", content="新正文")
        repos.MemoryChunkRepo(self.db).create(self.nid, kind="chapter_slice", source_chapter_id=chapter.id, text="旧正文", vector=[1.0])
        with patch("app.infrastructure.vector.EmbeddingService", side_effect=RuntimeError("无法初始化")):
            result = memory.write_chapter_memory(self.db, self.nid, chapter.id, chapter.content)
        self.assertEqual(result.as_dict(), {"written": 0, "total": 1, "failed": 1, "status": "embedding_failed"})
        self.assertTrue(result.warnings)
        self.assertEqual(repos.MemoryChunkRepo(self.db).list_by_novel(self.nid), [])

    def test_empty_body_does_not_initialize_embedding_or_report_service_failure(self):
        chapter = repos.ChapterRepo(self.db).create(self.nid, title="空章")
        with patch("app.infrastructure.vector.EmbeddingService", side_effect=RuntimeError("不可用")) as service:
            result = memory.write_chapter_memory(self.db, self.nid, chapter.id, " \n\t")
        service.assert_not_called()
        self.assertEqual(result.status, "empty")
        self.assertEqual(result.warnings, [])

    def test_search_keeps_current_normalized_slices_and_settings_but_skips_stale_and_foreign_sources(self):
        chapter = repos.ChapterRepo(self.db).create(self.nid, title="当前章", content=" 甲段 \n\n 乙段 ")
        foreign_novel = repos.NovelRepo(self.db).create(title="另一作品")
        foreign = repos.ChapterRepo(self.db).create(foreign_novel.id, title="异作品章", content="他作正文")
        for kind, source, text in (
            ("chapter_slice", chapter.id, "甲段\n乙段"),
            ("chapter_slice", chapter.id, "旧正文"),
            ("chapter_slice", "已删除的章节", "孤立正文"),
            ("chapter_slice", foreign.id, "他作正文"),
            ("setting", "", "人物设定"),
            ("chapter_slice", "", "无来源历史记忆"),
        ):
            repos.MemoryChunkRepo(self.db).create(self.nid, kind=kind, source_chapter_id=source, text=text, vector=[1.0, 0.0, 0.0])
        with patch("app.infrastructure.vector.EmbeddingService", return_value=FakeEmbeddingService()):
            results = vector.search(self.db, self.nid, "相关内容", top_k=10)
        self.assertEqual({r["text"] for r in results}, {"甲段\n乙段", "人物设定", "无来源历史记忆"})

    def test_settlement_reports_embedding_failure_without_losing_saved_state(self):
        chapter = repos.ChapterRepo(self.db).create(self.nid, title="第一章", content="新正文")
        repos.MemoryChunkRepo(self.db).create(self.nid, kind="chapter_slice", source_chapter_id=chapter.id, text="旧正文", vector=[1.0])
        with (
            patch("app.infrastructure.vector.EmbeddingService") as service,
            patch.object(settle.llm, "complete", return_value='{"summary":"定稿摘要"}'),
        ):
            service.return_value.embed.side_effect = RuntimeError("离线")
            result = settle.settle_chapter(self.db, self.nid, chapter.id)
        self.assertEqual(result["memory"]["status"], "embedding_failed")
        self.assertTrue(result["warnings"])
        stored = repos.ChapterRepo(self.db).get(self.nid, chapter.id)
        self.assertEqual((stored.status, stored.summary), ("finalized", "定稿摘要"))
        self.assertEqual(repos.MemoryChunkRepo(self.db).list_by_novel(self.nid), [])

    def test_settlement_reports_persistent_memory_database_failure_and_skips_old_recall(self):
        chapter = repos.ChapterRepo(self.db).create(self.nid, title="第一章", content="新正文")
        repos.MemoryChunkRepo(self.db).create(self.nid, kind="chapter_slice", source_chapter_id=chapter.id, text="旧正文", vector=[1.0])
        real_commit = self.db.commit
        commits = 0

        def fail_after_chapter_save():
            nonlocal commits
            commits += 1
            if commits > 1:
                raise RuntimeError("记忆库持续失败")
            return real_commit()

        with (
            patch("app.infrastructure.vector.EmbeddingService", return_value=FakeEmbeddingService()),
            patch.object(settle.llm, "complete", return_value='{"summary":"已保存摘要"}'),
            patch.object(self.db, "commit", side_effect=fail_after_chapter_save),
        ):
            result = settle.settle_chapter(self.db, self.nid, chapter.id)
        self.assertEqual(result["memory"]["status"], "storage_failed")
        self.assertIn("清理失败", result["warnings"][0])
        self.assertEqual(repos.ChapterRepo(self.db).get(self.nid, chapter.id).summary, "已保存摘要")
        self.assertEqual([c.text for c in repos.MemoryChunkRepo(self.db).list_by_novel(self.nid)], ["旧正文"])
        with patch("app.infrastructure.vector.EmbeddingService", return_value=FakeEmbeddingService()):
            self.assertEqual(vector.search(self.db, self.nid, "旧正文"), [])

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
