"""阶段一单元测试：Story Bible 七类实体的 CRUD 与关系操作。"""
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.infrastructure import models
from app.infrastructure.db import get_db
from app.main import app

# 用共享内存 SQLite，隔离真实数据库
engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
models.Base.metadata.create_all(bind=engine)
TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def override_get_db():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)


class StoryBibleTest(unittest.TestCase):
    def _create_novel(self, title="测试作品"):
        resp = client.post("/api/v1/novels", json={"title": title, "genre": "玄幻", "summary": "测试"})
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()

    def test_novel_crud(self):
        novel = self._create_novel()
        novel_id = novel["id"]
        self.assertEqual(novel["title"], "测试作品")

        resp = client.get("/api/v1/novels")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(any(n["id"] == novel_id for n in resp.json()))

        resp = client.get(f"/api/v1/novels/{novel_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["genre"], "玄幻")

        resp = client.put(f"/api/v1/novels/{novel_id}", json={"title": "改名后"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["title"], "改名后")

        resp = client.delete(f"/api/v1/novels/{novel_id}")
        self.assertEqual(resp.status_code, 204)

    def _entity_crud(self, path, create_payload, update_payload, check_field):
        novel = self._create_novel()
        nid = novel["id"]
        base = f"/api/v1/novels/{nid}/{path}"

        resp = client.post(base, json=create_payload)
        self.assertEqual(resp.status_code, 201, resp.text)
        item = resp.json()
        item_id = item["id"]
        self.assertEqual(item[check_field], create_payload[check_field])

        resp = client.get(base)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(any(x["id"] == item_id for x in resp.json()))

        resp = client.get(f"{base}/{item_id}")
        self.assertEqual(resp.status_code, 200)

        resp = client.put(f"{base}/{item_id}", json=update_payload)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()[check_field], update_payload[check_field])

        resp = client.delete(f"{base}/{item_id}")
        self.assertEqual(resp.status_code, 204)

    def test_character_crud(self):
        self._entity_crud(
            "characters",
            {"name": "林越", "role": "主角", "extras": {"阵营": "青云宗"}},
            {"name": "林越改"},
            "name",
        )

    def test_location_crud(self):
        self._entity_crud(
            "locations",
            {"name": "青云山", "level": "山脉"},
            {"name": "青云山改"},
            "name",
        )

    def test_world_setting_crud(self):
        self._entity_crud(
            "world-settings",
            {"name": "修炼体系", "category": "体系", "tags": ["修炼", "境界"]},
            {"name": "修炼体系改"},
            "name",
        )

    def test_timeline_event_crud(self):
        self._entity_crud(
            "timeline-events",
            {"title": "林越入门", "time_point": "第1章", "participants": ["p1"]},
            {"title": "林越入门改"},
            "title",
        )

    def test_foreshadowing_crud(self):
        self._entity_crud(
            "foreshadowings",
            {"content": "林越身世之谜", "status": "planted"},
            {"content": "林越身世之谜改"},
            "content",
        )

    def test_foreshadowing_invalid_status(self):
        novel = self._create_novel()
        resp = client.post(
            f"/api/v1/novels/{novel['id']}/foreshadowings",
            json={"content": "伏笔", "status": "invalid-status"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_create_under_missing_novel(self):
        resp = client.post(
            "/api/v1/novels/not-exist/characters",
            json={"name": "某人"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_relationship_create_list_delete(self):
        novel = self._create_novel()
        nid = novel["id"]

        c1 = client.post(f"/api/v1/novels/{nid}/characters", json={"name": "林越"}).json()
        c2 = client.post(f"/api/v1/novels/{nid}/characters", json={"name": "苏婉儿"}).json()

        resp = client.post(
            f"/api/v1/novels/{nid}/relationships",
            json={"subject_id": c1["id"], "object_id": c2["id"], "relation_type": "师徒"},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        rel = resp.json()
        self.assertEqual(rel["relation_type"], "师徒")

        resp = client.get(f"/api/v1/novels/{nid}/relationships")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(any(r["id"] == rel["id"] for r in resp.json()))

        resp = client.delete(f"/api/v1/novels/{nid}/relationships/{rel['id']}")
        self.assertEqual(resp.status_code, 204)


if __name__ == "__main__":
    unittest.main()
