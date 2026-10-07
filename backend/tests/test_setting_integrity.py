"""隔离内存库验收：关联归属、删除引用生命周期与原子失败。"""
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.application import setting_index
from app.infrastructure import models, repository as repos
from app.infrastructure.db import get_db
from app.main import app


class SettingIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        models.Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.sessions()
        self.nid = repos.NovelRepo(self.db).create(title="作品 A").id
        self.other = repos.NovelRepo(self.db).create(title="作品 B").id
        self.previous_override = app.dependency_overrides.get(get_db)

        def override_db():
            with self.sessions() as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        self.client = TestClient(app)
        self.embedding_patch = patch.object(setting_index.embedding, "embed", return_value=[[1.0, 0.0]])
        self.embedding = self.embedding_patch.start()

    def tearDown(self):
        self.embedding_patch.stop()
        if self.previous_override is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = self.previous_override
        self.db.close()
        self.engine.dispose()

    def create(self, path, payload, novel_id=None):
        response = self.client.post(f"/api/v1/novels/{novel_id or self.nid}/{path}", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def update(self, path, item_id, payload, novel_id=None):
        return self.client.put(f"/api/v1/novels/{novel_id or self.nid}/{path}/{item_id}", json=payload)

    def stored(self, novel_id):
        """逐字段比较作品数据，发现失败请求或删除是否污染另一部作品。"""
        self.db.expire_all()
        result = {}
        for table in models.Base.metadata.sorted_tables:
            if "novel_id" in table.c:
                rows = self.db.execute(select(table).where(table.c.novel_id == novel_id)).mappings()
                result[table.name] = sorted([dict(row) for row in rows], key=lambda row: row["id"])
        return result

    def chunk_text(self, item_id):
        self.db.expire_all()
        return self.db.query(models.MemoryChunk).filter_by(
            novel_id=self.nid, kind="setting", embedding_id=item_id,
        ).one().text

    def test_create_rejects_missing_and_foreign_references_without_writes(self):
        own = self.create("characters", {"name": "甲"})
        foreign_person = self.create("characters", {"name": "乙"}, self.other)
        foreign_place = self.create("locations", {"name": "异域"}, self.other)
        foreign_event = self.create("timeline-events", {"title": "异事"}, self.other)
        foreign_chapter = self.create("chapters", {"title": "异章"}, self.other)
        cases = [
            ("relationships", {"subject_id": own["id"], "object_id": foreign_person["id"], "relation_type": "师徒"}),
            ("relationships", {"subject_id": "missing", "object_id": own["id"], "relation_type": "師徒"}),
            ("locations", {"name": "本地", "parent_id": foreign_place["id"]}),
            ("locations", {"name": "本地", "parent_id": "missing"}),
            ("timeline-events", {"title": "本事", "cause_id": foreign_event["id"]}),
            ("timeline-events", {"title": "本事", "participants": [foreign_person["id"]]}),
            ("timeline-events", {"title": "本事", "participants": ["missing"]}),
            ("foreshadowings", {"content": "谜", "related_characters": [foreign_person["id"]]}),
            ("foreshadowings", {"content": "谜", "planted_chapter_id": foreign_chapter["id"]}),
            ("foreshadowings", {"content": "谜", "consumed_chapter_id": "missing"}),
        ]
        before = self.stored(self.nid), self.stored(self.other)
        for path, payload in cases:
            with self.subTest(path=path, payload=payload):
                response = self.client.post(f"/api/v1/novels/{self.nid}/{path}", json=payload)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("detail", response.json())
                self.assertEqual((self.stored(self.nid), self.stored(self.other)), before)

    def test_malformed_reference_lists_return_400(self):
        for path, payload in (
            ("timeline-events", {"title": "事件", "participants": [{"id": "missing"}]}),
            ("foreshadowings", {"content": "谜", "related_characters": [None]}),
            ("timeline-events", {"title": "事件", "participants": [23]}),
        ):
            with self.subTest(path=path, payload=payload):
                response = self.client.post(f"/api/v1/novels/{self.nid}/{path}", json=payload)
                self.assertEqual(response.status_code, 400, response.text)

    def test_valid_references_can_be_created_and_cleared(self):
        person = self.create("characters", {"name": "甲"})
        place = self.create("locations", {"name": "城"})
        child = self.create("locations", {"name": "屋", "parent_id": place["id"]})
        cause = self.create("timeline-events", {"title": "先因", "participants": [person["id"]]})
        event = self.create("timeline-events", {"title": "后果", "cause_id": cause["id"], "participants": [person["id"]]})
        chapter = self.create("chapters", {"title": "第一章"})
        clue = self.create("foreshadowings", {"content": "玉佩", "related_characters": [person["id"]],
                                             "planted_chapter_id": chapter["id"], "consumed_chapter_id": chapter["id"]})
        for path, item, payload in (
            ("locations", child, {"parent_id": ""}),
            ("timeline-events", event, {"participants": [], "cause_id": ""}),
            ("foreshadowings", clue, {"related_characters": [], "planted_chapter_id": "", "consumed_chapter_id": ""}),
        ):
            response = self.update(path, item["id"], payload)
            self.assertEqual(response.status_code, 200, response.text)
            for field, value in payload.items():
                self.assertEqual(response.json()[field], value)

    def test_failed_update_keeps_entity_and_old_index(self):
        place = self.create("locations", {"name": "原名"})
        person = self.create("characters", {"name": "原人物"})
        event = self.create("timeline-events", {"title": "原事", "participants": [person["id"]]})
        clue = self.create("foreshadowings", {"content": "原谜", "related_characters": [person["id"]]})
        for path, item, payload in (
            ("locations", place, {"name": "不应保存", "parent_id": "missing"}),
            ("timeline-events", event, {"title": "不应保存", "participants": ["missing"]}),
            ("timeline-events", event, {"cause_id": "missing"}),
            ("foreshadowings", clue, {"content": "不应保存", "related_characters": ["missing"]}),
            ("foreshadowings", clue, {"planted_chapter_id": "missing"}),
            ("foreshadowings", clue, {"consumed_chapter_id": "missing"}),
        ):
            with self.subTest(path=path, payload=payload):
                before = self.stored(self.nid)
                response = self.update(path, item["id"], payload)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self.stored(self.nid), before)

    def test_location_and_timeline_chains_reject_self_and_indirect_cycles(self):
        for path, title_field, link_field in (("locations", "name", "parent_id"),
                                              ("timeline-events", "title", "cause_id")):
            first = self.create(path, {title_field: "第一"})
            second = self.create(path, {title_field: "第二", link_field: first["id"]})
            for target in (first["id"], second["id"]):
                with self.subTest(path=path, target=target):
                    before = self.stored(self.nid)
                    response = self.update(path, first["id"], {link_field: target})
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertIn("循环", response.json()["detail"])
                    self.assertEqual(self.stored(self.nid), before)

    def test_relationships_accept_all_graph_node_types_and_refresh_names(self):
        person = self.create("characters", {"name": "甲"})
        for path, field, title in (("locations", "name", "旧城"), ("world-settings", "name", "旧势力"),
                                   ("timeline-events", "title", "旧事"), ("foreshadowings", "content", "旧谜")):
            endpoint = self.create(path, {field: title})
            relation = self.create("relationships", {"subject_id": person["id"], "object_id": endpoint["id"],
                                                       "relation_type": "关联"})
            self.assertIn(title, self.chunk_text(relation["id"]))
            response = self.update(path, endpoint["id"], {field: "新名称"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("新名称", self.chunk_text(relation["id"]))
            self.assertNotIn(title, self.chunk_text(relation["id"]))

    def test_delete_character_unlinks_current_novel_and_preserves_other_novel(self):
        person = self.create("characters", {"name": "要删除的甲"})
        retained = self.create("characters", {"name": "留下的乙"})
        event = self.create("timeline-events", {"title": "事件", "participants": [person["id"], retained["id"]]})
        clue = self.create("foreshadowings", {"content": "谜", "related_characters": [person["id"], retained["id"]]})
        relation = self.create("relationships", {"subject_id": person["id"], "object_id": retained["id"], "relation_type": "朋友"})
        # 兼容已经存在的历史坏引用，删除 A 时不得改 B 的设定或索引。
        foreign_event = repos.TimelineEventRepo(self.db).create(self.other, title="另一作品历史记录", participants=[person["id"]])
        foreign_clue = repos.ForeshadowingRepo(self.db).create(self.other, content="另一作品旧谜", related_characters=[person["id"]])
        foreign_relation = repos.RelationshipRepo(self.db).create(self.other, subject_id=person["id"], object_id=retained["id"], relation_type="旧关系")
        for item in (foreign_event, foreign_clue, foreign_relation):
            repos.MemoryChunkRepo(self.db).create(self.other, kind="setting", embedding_id=item.id, text="保持原样", vector=[1.0])
        before = self.stored(self.other)
        response = self.client.delete(f"/api/v1/novels/{self.nid}/characters/{person['id']}")
        self.assertEqual(response.status_code, 204, response.text)
        self.db.expire_all()
        self.assertEqual(repos.TimelineEventRepo(self.db).get(self.nid, event["id"]).participants, [retained["id"]])
        self.assertEqual(repos.ForeshadowingRepo(self.db).get(self.nid, clue["id"]).related_characters, [retained["id"]])
        self.assertNotIn(person["id"], self.chunk_text(event["id"]))
        self.assertNotIn(person["id"], self.chunk_text(clue["id"]))
        self.assertIn("人物已删除", self.chunk_text(relation["id"]))
        self.assertNotIn("要删除的甲", self.chunk_text(relation["id"]))
        self.assertEqual(self.stored(self.other), before)

    def test_delete_place_and_event_unlinks_child_and_refreshes_index(self):
        for path, title_field, link_field, repo in (
            ("locations", "name", "parent_id", repos.LocationRepo),
            ("timeline-events", "title", "cause_id", repos.TimelineEventRepo),
        ):
            parent = self.create(path, {title_field: "前序"})
            child = self.create(path, {title_field: "后序", link_field: parent["id"]})
            repo(self.db).create(self.other, **{title_field: "旧坏引用", link_field: parent["id"]})
            before = self.stored(self.other)
            response = self.client.delete(f"/api/v1/novels/{self.nid}/{path}/{parent['id']}")
            self.assertEqual(response.status_code, 204, response.text)
            self.db.expire_all()
            self.assertEqual(getattr(repo(self.db).get(self.nid, child["id"]), link_field), "")
            self.assertNotIn(parent["id"], self.chunk_text(child["id"]))
            self.assertEqual(self.stored(self.other), before)

    def test_delete_chapter_invalidates_changed_foreshadowing_indexes(self):
        chapter = self.create("chapters", {"title": "第一章"})
        clue = self.create("foreshadowings", {"content": "玉佩", "planted_chapter_id": chapter["id"],
                                             "consumed_chapter_id": chapter["id"], "status": "consumed"})
        foreign_clue = repos.ForeshadowingRepo(self.db).create(self.other, content="另一作品旧引用", planted_chapter_id=chapter["id"])
        repos.MemoryChunkRepo(self.db).create(self.other, kind="setting", embedding_id=foreign_clue.id, text="原文", vector=[1.0])
        before = self.stored(self.other)
        response = self.client.delete(f"/api/v1/novels/{self.nid}/chapters/{chapter['id']}")
        self.assertEqual(response.status_code, 204, response.text)
        self.db.expire_all()
        saved = repos.ForeshadowingRepo(self.db).get(self.nid, clue["id"])
        self.assertEqual((saved.planted_chapter_id, saved.consumed_chapter_id, saved.status), ("", "", "consumed"))
        self.assertEqual(self.db.query(models.MemoryChunk).filter_by(novel_id=self.nid, kind="setting", embedding_id=clue["id"]).count(), 0)
        self.assertEqual(self.stored(self.other), before)

    def test_chapter_crud_rejects_foreign_missing_and_nonchapter_outline_bindings(self):
        volume = self.create("outline", {"title": "卷", "level": "volume"})
        act = self.create("outline", {"title": "幕", "level": "act", "parent_id": volume["id"]})
        node = self.create("outline", {"title": "章", "level": "chapter", "parent_id": act["id"]})
        foreign_volume = self.create("outline", {"title": "异卷", "level": "volume"}, self.other)
        foreign_node = self.create("outline", {"title": "异章", "level": "chapter", "parent_id": foreign_volume["id"]}, self.other)
        chapter = self.create("chapters", {"title": "正文章", "content": "原正文", "outline_node_id": node["id"]})
        before = self.stored(self.nid), self.stored(self.other)
        for invalid in ("missing", foreign_node["id"], volume["id"], act["id"]):
            with self.subTest(outline_node_id=invalid):
                response = self.client.post(f"/api/v1/novels/{self.nid}/chapters", json={"outline_node_id": invalid})
                self.assertEqual(response.status_code, 400, response.text)
                response = self.update("chapters", chapter["id"], {"content": "不可保存", "outline_node_id": invalid})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual((self.stored(self.nid), self.stored(self.other)), before)
        self.assertEqual(self.update("chapters", chapter["id"], {"outline_node_id": ""}).status_code, 200)
        response = self.update("chapters", chapter["id"], {"outline_node_id": node["id"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["outline_node_id"], node["id"])

    def test_cross_novel_update_and_delete_still_return_404(self):
        place = self.create("locations", {"name": "本作品地点"})
        chapter = self.create("chapters", {"title": "本作品章节"})
        before = self.stored(self.nid), self.stored(self.other)
        for path, item in (("locations", place), ("chapters", chapter)):
            response = self.update(path, item["id"], {"outline_node_id": "missing"} if path == "chapters" else {"name": "不可保存"}, self.other)
            self.assertEqual(response.status_code, 404, response.text)
            response = self.client.delete(f"/api/v1/novels/{self.other}/{path}/{item['id']}")
            self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual((self.stored(self.nid), self.stored(self.other)), before)


if __name__ == "__main__":
    unittest.main()
