"""方向 B 设定索引：同步替换、失败失效、显式重建。"""
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.application import setting_index
from app.infrastructure import models
from app.infrastructure.repository import CharacterRepo, NovelRepo, RelationshipRepo


class SettingIndexTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        models.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, autoflush=False)()
        self.novel = NovelRepo(self.db).create(title="测试作品")

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def chunks(self, kind="setting"):
        return self.db.query(models.MemoryChunk).filter(
            models.MemoryChunk.novel_id == self.novel.id,
            models.MemoryChunk.kind == kind,
        ).all()

    def test_create_update_delete_never_leaves_old_setting(self):
        with patch.object(setting_index.embedding, "embed", side_effect=lambda texts: [[1.0, 0.0] for _ in texts]):
            person = setting_index.create_setting(
                self.db, CharacterRepo, self.novel.id, name="吴迪", personality="胆小",
            )
            self.assertEqual(len(self.chunks()), 1)
            self.assertIn("胆小", self.chunks()[0].text)
            self.assertEqual(self.chunks()[0].embedding_id, person.id)

            setting_index.update_setting(
                self.db, CharacterRepo, self.novel.id, person.id, personality="勇敢",
            )
            self.assertEqual(len(self.chunks()), 1)
            self.assertIn("勇敢", self.chunks()[0].text)
            self.assertNotIn("胆小", self.chunks()[0].text)

            setting_index.delete_setting(self.db, CharacterRepo, self.novel.id, person.id)
            self.assertEqual(self.chunks(), [])

    def test_embed_failure_removes_old_index_but_keeps_entity_update(self):
        with patch.object(setting_index.embedding, "embed", side_effect=lambda texts: [[1.0] for _ in texts]):
            person = setting_index.create_setting(
                self.db, CharacterRepo, self.novel.id, name="吴迪", goal="发财",
            )
        with patch.object(setting_index.embedding, "embed", side_effect=RuntimeError("离线")):
            setting_index.update_setting(
                self.db, CharacterRepo, self.novel.id, person.id, goal="救人",
            )
        self.assertEqual(CharacterRepo(self.db).get(self.novel.id, person.id).goal, "救人")
        self.assertEqual(self.chunks(), [])

    def test_rebuild_is_idempotent_and_keeps_chapter_slices(self):
        person = CharacterRepo(self.db).create(self.novel.id, name="吴迪")
        self.db.add(models.MemoryChunk(
            novel_id=self.novel.id, kind="chapter_slice", source_chapter_id="chapter-id",
            text="正文片段", vector=[1.0],
        ))
        self.db.commit()
        with patch.object(setting_index.embedding, "embed", side_effect=lambda texts: [[1.0] for _ in texts]):
            first = setting_index.rebuild_settings_index(self.db, self.novel.id)
            second = setting_index.rebuild_settings_index(self.db, self.novel.id)
        self.assertEqual(first, {"indexed": 1, "failed": 0})
        self.assertEqual(first, second)
        self.assertEqual(len(self.chunks()), 1)
        self.assertEqual(self.chunks()[0].embedding_id, person.id)
        self.assertEqual(len(self.chunks("chapter_slice")), 1)

    def test_relationship_index_uses_current_character_names(self):
        with patch.object(setting_index.embedding, "embed", side_effect=lambda texts: [[1.0] for _ in texts]):
            first = setting_index.create_setting(
                self.db, CharacterRepo, self.novel.id, name="吴迪",
            )
            second = setting_index.create_setting(
                self.db, CharacterRepo, self.novel.id, name="小林",
            )
            relation = setting_index.create_setting(
                self.db, RelationshipRepo, self.novel.id,
                subject_id=first.id, object_id=second.id, relation_type="朋友",
            )
            chunk = next(c for c in self.chunks() if c.embedding_id == relation.id)
            self.assertIn("吴迪", chunk.text)
            self.assertIn("小林", chunk.text)
            self.assertNotIn(first.id, chunk.text)

            setting_index.update_setting(
                self.db, CharacterRepo, self.novel.id, first.id, name="吴明",
            )
            chunk = next(c for c in self.chunks() if c.embedding_id == relation.id)
            self.assertIn("吴明", chunk.text)
            self.assertNotIn("吴迪", chunk.text)

            setting_index.delete_setting(self.db, CharacterRepo, self.novel.id, first.id)
            chunk = next(c for c in self.chunks() if c.embedding_id == relation.id)
            self.assertIn("人物已删除", chunk.text)
            self.assertNotIn("吴明", chunk.text)


if __name__ == "__main__":
    unittest.main()
