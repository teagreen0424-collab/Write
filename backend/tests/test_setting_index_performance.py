"""高关联设定：索引成本有界、向量对应正确、故障仍保护作者数据。"""
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import sessionmaker

from app.application import setting_index
from app.infrastructure import models, repository as repos
from app.interfaces import schemas


def vectors(texts):
    return [[float(sum(map(ord, text))), 1.0] for text in texts]


class SettingIndexPerformanceTest(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="lingmo-setting-index-")
        self.addCleanup(self.directory.cleanup)
        self.engine = create_engine(f"sqlite:///{(Path(self.directory.name) / 'index.sqlite').as_posix()}")
        models.Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        embedding_patch = patch.object(setting_index.embedding, "embed", side_effect=vectors)
        self.embed = embedding_patch.start()
        self.addCleanup(embedding_patch.stop)
        self.nid = repos.NovelRepo(self.db).create(title="本作品").id
        self.other = repos.NovelRepo(self.db).create(title="其他作品").id
        self.statements = []
        event.listen(self.engine, "before_cursor_execute", lambda _conn, _cursor, statement, *_args: self.statements.append(statement))

    def seed_hub(self, count=100):
        hub = models.Character(novel_id=self.nid, name="主角", goal="旧目标")
        places = [models.Location(novel_id=self.nid, name=f"地点{i:03}") for i in range(count)]
        self.db.add_all([hub, *places]); self.db.flush()
        relations = [models.Relationship(novel_id=self.nid, subject_id=hub.id, object_id=place.id, relation_type="相识") for place in places]
        self.db.add_all(relations); self.db.flush()
        self.db.add(models.MemoryChunk(novel_id=self.nid, kind="setting", embedding_id=hub.id,
                    text="人物；name：主角；goal：旧目标", vector=[1.0, 1.0]))
        for relation, place in zip(relations, places):
            self.db.add(models.MemoryChunk(novel_id=self.nid, kind="setting", embedding_id=relation.id,
                text=f"关系；subject_id：主角；object_id：{place.name}；relation_type：相识", vector=[2.0, 1.0]))
        ids = hub.id, [relation.id for relation in relations], [place.id for place in places]
        self.db.commit()
        self.embed.reset_mock(); self.statements.clear()
        return ids

    def chunks(self, nid=None, kind="setting"):
        return {row.embedding_id: (row.id, row.text, row.vector) for row in self.db.query(models.MemoryChunk)
                .filter_by(novel_id=nid or self.nid, kind=kind).all()}

    def test_goal_and_personality_edits_preserve_all_100_relationship_indices(self):
        hub, relations, _ = self.seed_hub()
        before = self.chunks()
        self.statements.clear()
        setting_index.update_setting(self.db, repos.CharacterRepo, self.nid, hub, goal="新目标", personality="勇敢")
        write_sql = len(self.statements)
        after = self.chunks()
        self.assertEqual({rid: after[rid] for rid in relations}, {rid: before[rid] for rid in relations})
        self.assertIn("新目标", after[hub][1])
        self.assertIn("勇敢", after[hub][1])
        self.embed.assert_called_once()
        self.assertLessEqual(write_sql, 8)

    def test_unchanged_display_name_does_not_reindex_relationships(self):
        hub, relations, _ = self.seed_hub()
        before = self.chunks()
        setting_index.update_setting(self.db, repos.CharacterRepo, self.nid, hub, name="主角", goal="新目标")
        after = self.chunks()
        self.assertEqual({rid: after[rid] for rid in relations}, {rid: before[rid] for rid in relations})
        self.embed.assert_called_once()

    def test_100_relationship_rename_batches_embeddings_and_endpoint_queries(self):
        hub, relations, _ = self.seed_hub()
        setting_index.update_setting(self.db, repos.CharacterRepo, self.nid, hub, name="新主角")
        write_sql = len(self.statements)
        after = self.chunks()
        self.assertEqual(self.embed.call_count, 2)
        self.assertEqual(sorted(len(call.args[0]) for call in self.embed.call_args_list), [1, 100])
        self.assertLessEqual(write_sql, 25)
        endpoint_reads = [statement for statement in self.statements if "SELECT " in statement and " IN (" in statement
                          and any(f"FROM {model.__tablename__}" in statement for model in setting_index._NODE_MODELS)]
        self.assertEqual(len(endpoint_reads), 5)
        for rid in relations:
            self.assertIn("新主角", after[rid][1])
            self.assertNotIn("subject_id：主角", after[rid][1])
            self.assertEqual(after[rid][2], vectors([after[rid][1]])[0])

    def test_each_graph_node_display_field_refreshes_related_text(self):
        other = repos.CharacterRepo(self.db).create(self.nid, name="对方").id
        variants = [(repos.CharacterRepo, "name"), (repos.LocationRepo, "name"), (repos.WorldSettingRepo, "name"),
                    (repos.TimelineEventRepo, "title"), (repos.ForeshadowingRepo, "content")]
        for repo, field in variants:
            with self.subTest(type=repo.model.__tablename__):
                obj = setting_index.create_setting(self.db, repo, self.nid, **{field: "旧显示名"})
                oid = obj.id
                relation = setting_index.create_setting(self.db, repos.RelationshipRepo, self.nid,
                    subject_id=oid, object_id=other, relation_type="相关")
                rid = relation.id
                setting_index.update_setting(self.db, repo, self.nid, oid, **{field: "新显示名"})
                self.assertIn("新显示名", self.chunks()[rid][1])
                self.assertNotIn("旧显示名", self.chunks()[rid][1])

    def test_short_or_extra_batch_result_never_assigns_vectors_to_wrong_entities(self):
        for extra in (-1, 1):
            with self.subTest(extra=extra):
                hub, relations, _ = self.seed_hub(3)
                self.embed.side_effect = lambda texts: vectors(texts) if len(texts) == 1 else [[1.0]] * (len(texts) + extra)
                setting_index.update_setting(self.db, repos.CharacterRepo, self.nid, hub, name="已保存新名字")
                self.assertEqual(repos.CharacterRepo(self.db).get(self.nid, hub).name, "已保存新名字")
                self.assertTrue(all(rid not in self.chunks() for rid in relations))
                self.assertIn(hub, self.chunks())
                self.embed.side_effect = vectors

    def test_embedding_error_clears_every_old_related_index_but_keeps_author_update(self):
        hub, relations, _ = self.seed_hub(12)

        def fail_batch(texts):
            if len(texts) > 1: raise RuntimeError("离线")
            return vectors(texts)

        self.embed.side_effect = fail_batch
        setting_index.update_setting(self.db, repos.CharacterRepo, self.nid, hub, name="已保存新名字")
        after = self.chunks()
        self.assertTrue(all(rid not in after for rid in relations))
        self.assertIn("已保存新名字", after[hub][1])
        self.assertEqual(self.db.query(models.Relationship).filter_by(novel_id=self.nid).count(), 12)

    def test_empty_vector_only_invalidates_its_own_index_without_shift(self):
        hub, relations, places = self.seed_hub(3)
        self.embed.side_effect = lambda texts: [[] if "地点001" in text else vectors([text])[0] for text in texts]
        setting_index.update_setting(self.db, repos.CharacterRepo, self.nid, hub, name="新名字")
        after = self.chunks()
        self.assertNotIn(relations[1], after)
        for rid in (relations[0], relations[2]):
            self.assertIn(rid, after)
            self.assertEqual(after[rid][2], vectors([after[rid][1]])[0])
        self.assertEqual(repos.RelationshipRepo(self.db).get(self.nid, relations[1]).object_id, places[1])

    def test_invalid_nonfinite_vector_is_not_saved_as_a_successful_index(self):
        hub, relations, _ = self.seed_hub(3)
        self.embed.side_effect = lambda texts: [[float("nan")] for _ in texts]
        setting_index.update_setting(self.db, repos.CharacterRepo, self.nid, hub, name="已保存名字")
        after = self.chunks()
        self.assertNotIn(hub, after)
        self.assertTrue(all(rid not in after for rid in relations))
        self.assertEqual(repos.CharacterRepo(self.db).get(self.nid, hub).name, "已保存名字")

    def test_delete_hub_keeps_100_relationships_with_missing_endpoint_label(self):
        hub, relations, _ = self.seed_hub()
        setting_index.delete_setting(self.db, repos.CharacterRepo, self.nid, hub)
        write_sql = len(self.statements)
        after = self.chunks()
        self.assertNotIn(hub, after)
        self.assertEqual(self.db.query(models.Relationship).filter_by(novel_id=self.nid).count(), 100)
        self.embed.assert_called_once()
        self.assertEqual(len(self.embed.call_args.args[0]), 100)
        self.assertLessEqual(write_sql, 25)
        for rid in relations:
            self.assertIn("人物已删除", after[rid][1])
            self.assertNotIn("主角", after[rid][1])

    def test_delete_batch_reindexes_unlinked_events_and_foreshadowings(self):
        hub, relations, _ = self.seed_hub(3)
        evt = repos.TimelineEventRepo(self.db).create(self.nid, title="事件", participants=[hub])
        eid = evt.id
        clue = repos.ForeshadowingRepo(self.db).create(self.nid, content="伏笔", related_characters=[hub])
        fid = clue.id
        for oid in (eid, fid):
            self.db.add(models.MemoryChunk(novel_id=self.nid, kind="setting", embedding_id=oid, text=f"旧引用{hub}", vector=[1.0]))
        self.db.commit(); self.embed.reset_mock()
        setting_index.delete_setting(self.db, repos.CharacterRepo, self.nid, hub)
        self.assertEqual(repos.TimelineEventRepo(self.db).get(self.nid, eid).participants, [])
        self.assertEqual(repos.ForeshadowingRepo(self.db).get(self.nid, fid).related_characters, [])
        after = self.chunks()
        self.assertTrue(all(oid in after for oid in [eid, fid, *relations]))
        self.assertTrue(all(hub not in after[oid][1] for oid in (eid, fid)))
        self.assertLessEqual(self.embed.call_count, 2)

    def test_related_refresh_is_scoped_and_preserves_chapter_slices(self):
        hub, relations, _ = self.seed_hub(3)
        foreign = models.Character(novel_id=self.other, name="外国人物")
        self.db.add(foreign); self.db.flush()
        # 旧库即使存在另一作品误指向本作品ID，也不可被本作品索引刷新波及。
        foreign_relation = models.Relationship(novel_id=self.other, subject_id=hub, object_id=foreign.id, relation_type="旧库关联")
        self.db.add(foreign_relation); self.db.flush()
        self.db.add_all([models.MemoryChunk(novel_id=self.other, kind="setting", embedding_id=foreign_relation.id, text="外国旧索引", vector=[9.0]),
            models.MemoryChunk(novel_id=self.other, kind="setting", embedding_id=hub, text="外国同ID旧索引", vector=[8.0]),
            models.MemoryChunk(novel_id=self.nid, kind="chapter_slice", source_chapter_id="chapter", embedding_id=relations[0], text="章节正文", vector=[7.0])])
        self.db.commit()
        before_foreign, before_slices = self.chunks(self.other), self.chunks(kind="chapter_slice")
        setting_index.update_setting(self.db, repos.CharacterRepo, self.nid, hub, name="新主角")
        setting_index.delete_setting(self.db, repos.CharacterRepo, self.nid, hub)
        self.assertEqual(self.chunks(self.other), before_foreign)
        self.assertEqual(self.chunks(kind="chapter_slice"), before_slices)

    def test_flush_and_commit_failure_roll_back_name_and_old_indices_together(self):
        for mode in ("flush", "commit"):
            with self.subTest(mode=mode):
                hub, _, _ = self.seed_hub(3)
                before = self.chunks()
                original_flush = self.db.flush

                def fail_index_flush(*args, **kwargs):
                    if any(isinstance(obj, models.MemoryChunk) for obj in self.db.new):
                        raise RuntimeError("索引写库失败")
                    return original_flush(*args, **kwargs)

                fault = patch.object(self.db, "flush", side_effect=fail_index_flush) if mode == "flush" else patch.object(self.db, "commit", side_effect=RuntimeError("提交失败"))
                with fault, self.assertRaises(RuntimeError):
                    setting_index.update_setting(self.db, repos.CharacterRepo, self.nid, hub, name="不能落库")
                self.assertEqual(repos.CharacterRepo(self.db).get(self.nid, hub).name, "主角")
                self.assertEqual(self.chunks(), before)

    def test_atomic_caller_can_rollback_own_and_related_index_changes(self):
        hub, _, _ = self.seed_hub(3)
        before = self.chunks()
        setting_index.update_setting(self.db, repos.CharacterRepo, self.nid, hub, name="原子未提交名字", commit=False)
        with self.Session() as outside:
            self.assertEqual(repos.CharacterRepo(outside).get(self.nid, hub).name, "主角")
        self.db.rollback()
        self.assertEqual(repos.CharacterRepo(self.db).get(self.nid, hub).name, "主角")
        self.assertEqual(self.chunks(), before)

    def test_create_without_commit_remains_part_of_the_callers_transaction(self):
        with patch.object(self.db, "commit", wraps=self.db.commit) as commit:
            created = setting_index.create_setting(self.db, repos.CharacterRepo, self.nid, name="未提交新人物", commit=False)
            commit.assert_not_called()
        cid = created.id
        self.assertIs(self.db.get(models.Character, cid), created)
        self.assertTrue(any(isinstance(obj, models.MemoryChunk) for obj in self.db.new))
        with self.Session() as outside:
            self.assertIsNone(outside.get(models.Character, cid))
        self.db.rollback()
        with self.Session() as outside:
            self.assertIsNone(outside.get(models.Character, cid))
            self.assertEqual(outside.query(models.MemoryChunk).filter_by(novel_id=self.nid, embedding_id=cid).count(), 0)

    def test_rebuild_batches_all_entities_and_counts_partial_failures_without_touching_slices(self):
        _, _, _ = self.seed_hub()
        self.db.add_all([models.MemoryChunk(novel_id=self.nid, kind="chapter_slice", source_chapter_id="chapter", text="正文", vector=[4.0]),
                        models.MemoryChunk(novel_id=self.other, kind="setting", embedding_id="foreign", text="外国索引", vector=[8.0])])
        self.db.commit()
        slices, foreign = self.chunks(kind="chapter_slice"), self.chunks(self.other)
        self.embed.side_effect = lambda texts: [[] if "地点001" in text else vectors([text])[0] for text in texts]
        result = setting_index.rebuild_settings_index(self.db, self.nid)
        self.assertEqual(result, {"indexed": 199, "failed": 2})
        self.assertLessEqual(self.embed.call_count, 3)
        after = self.chunks()
        self.assertEqual(len(after), 199)
        self.assertTrue(all(values[2] == vectors([values[1]])[0] for values in after.values()))
        self.assertEqual(self.chunks(kind="chapter_slice"), slices)
        self.assertEqual(self.chunks(self.other), foreign)
        self.assertEqual(setting_index.rebuild_settings_index(self.db, self.nid), result)
        self.assertEqual(len(self.chunks()), 199)

    def test_rebuild_wrong_length_clears_old_indices_and_reports_every_failure(self):
        self.seed_hub(3)
        self.embed.side_effect = lambda texts: vectors(texts)[:-1]
        self.assertEqual(setting_index.rebuild_settings_index(self.db, self.nid), {"indexed": 0, "failed": 7})
        self.assertEqual(self.chunks(), {})

    def test_empty_rebuild_clears_orphaned_setting_indices_without_calling_model(self):
        self.db.add(models.MemoryChunk(novel_id=self.nid, kind="setting", embedding_id="已删除记录", text="孤儿索引", vector=[1.0]))
        self.db.commit()
        self.assertEqual(setting_index.rebuild_settings_index(self.db, self.nid), {"indexed": 0, "failed": 0})
        self.embed.assert_not_called()
        self.assertEqual(self.chunks(), {})

    def test_rebuild_database_failure_restores_old_indices(self):
        self.seed_hub(3)
        before = self.chunks()
        with patch.object(self.db, "commit", side_effect=RuntimeError("提交失败")), self.assertRaises(RuntimeError):
            setting_index.rebuild_settings_index(self.db, self.nid)
        self.assertEqual(self.chunks(), before)

    def test_real_post_commit_exclusive_lock_still_returns_created_and_updated_identity(self):
        path = Path(self.directory.name) / "index.sqlite"
        timed_engine = create_engine(f"sqlite:///{path.as_posix()}", connect_args={"timeout": .02})
        TimedSession = sessionmaker(bind=timed_engine, autoflush=False)
        try:
            for action in ("create", "update"):
                with self.subTest(action=action), TimedSession() as db:
                    existing_id = repos.CharacterRepo(self.db).create(self.nid, name="原人物").id if action == "update" else None
                    identity = repos.CharacterRepo(db).get(self.nid, existing_id) if existing_id else None
                    blocker = sqlite3.connect(path, timeout=.02, isolation_level=None)
                    real_commit = db.commit

                    def commit_then_lock():
                        real_commit()
                        blocker.execute("BEGIN EXCLUSIVE")

                    try:
                        with patch.object(db, "commit", side_effect=commit_then_lock):
                            if action == "create":
                                saved = setting_index.create_setting(db, repos.CharacterRepo, self.nid, name="已创建人物")
                            else:
                                saved = setting_index.update_setting(db, repos.CharacterRepo, self.nid, existing_id, name="已更新人物")
                                self.assertIs(saved, identity)
                            response = schemas.CharacterRead.model_validate(saved).model_dump()
                            self.assertIs(db.get(models.Character, saved.id), saved)
                            self.assertTrue(inspect(saved).persistent)
                            self.assertFalse(inspect(saved).expired)
                            self.assertEqual(list(db.dirty), [])
                    finally:
                        blocker.rollback(); blocker.close()
                    with self.Session() as check:
                        self.assertEqual(schemas.CharacterRead.model_validate(check.get(models.Character, response["id"])).model_dump(), response)
                        self.assertEqual(check.query(models.MemoryChunk).filter_by(novel_id=self.nid, embedding_id=response["id"], kind="setting").count(), 1)
        finally:
            timed_engine.dispose()
