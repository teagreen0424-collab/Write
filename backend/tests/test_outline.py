"""阶段三单元测试：大纲 CRUD/重排/层级校验。"""
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.infrastructure import models
from app.infrastructure.db import get_db
from app.main import app

engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
models.Base.metadata.create_all(bind=engine)
Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def override_get_db():
    db = Session()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)


class OutlineApiTest(unittest.TestCase):
    def _create_novel(self):
        r = client.post("/api/v1/novels", json={"title": "测试作品", "genre": "玄幻"})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["id"]

    def test_outline_crud_and_reorder(self):
        nid = self._create_novel()
        base = f"/api/v1/novels/{nid}/outline"

        vol = client.post(base, json={"title": "第一卷", "level": "volume"}).json()
        act = client.post(base, json={"title": "第一幕", "level": "act", "parent_id": vol["id"]}).json()
        c1 = client.post(base, json={"title": "第一章", "level": "chapter", "parent_id": act["id"]}).json()
        c2 = client.post(base, json={"title": "第二章", "level": "chapter", "parent_id": act["id"]}).json()

        nodes = client.get(base).json()
        self.assertEqual(len(nodes), 4)

        # 同级重排：c1,c2 -> c2,c1
        r = client.post(f"{base}/reorder", json={"ordered_ids": [c2["id"], c1["id"]]})
        self.assertEqual(r.status_code, 200, r.text)
        nodes = {n["id"]: n for n in client.get(base).json()}
        self.assertLess(nodes[c2["id"]]["sort_order"], nodes[c1["id"]]["sort_order"])

    def test_outline_list_follows_reading_order_across_acts(self):
        nid = self._create_novel()
        base = f"/api/v1/novels/{nid}/outline"

        vol = client.post(base, json={"title": "第一卷", "level": "volume"}).json()
        act1 = client.post(base, json={"title": "第一幕", "level": "act", "parent_id": vol["id"]}).json()
        act2 = client.post(base, json={"title": "第二幕", "level": "act", "parent_id": vol["id"]}).json()
        chapters = []
        for act in (act1, act2):
            for index in range(2):
                chapter = client.post(base, json={
                    "title": f"{act['title']}第{index + 1}章",
                    "level": "chapter",
                    "parent_id": act["id"],
                }).json()
                chapters.append(chapter)

        r = client.get(base)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            [node["id"] for node in r.json()],
            [vol["id"], act1["id"], chapters[0]["id"], chapters[1]["id"],
             act2["id"], chapters[2]["id"], chapters[3]["id"]],
        )

        r = client.post(f"{base}/reorder", json={"ordered_ids": [act2["id"], act1["id"]]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            [node["id"] for node in client.get(base).json() if node["level"] == "chapter"],
            [chapters[2]["id"], chapters[3]["id"], chapters[0]["id"], chapters[1]["id"]],
        )

    def test_hierarchy_validation(self):
        nid = self._create_novel()
        base = f"/api/v1/novels/{nid}/outline"

        # 章节点不能无父
        r = client.post(base, json={"title": "章", "level": "chapter", "parent_id": ""})
        self.assertEqual(r.status_code, 400)
        # 幕节点不能无父
        r = client.post(base, json={"title": "幕", "level": "act", "parent_id": ""})
        self.assertEqual(r.status_code, 400)
        # 卷下建章合法
        vol = client.post(base, json={"title": "卷", "level": "volume"}).json()
        ch = client.post(base, json={"title": "章", "level": "chapter", "parent_id": vol["id"]})
        self.assertEqual(ch.status_code, 201)
        # 章下不能再建章
        r = client.post(base, json={"title": "章2", "level": "chapter", "parent_id": ch.json()["id"]})
        self.assertEqual(r.status_code, 400)

    def test_delete_node_keeps_chapter(self):
        nid = self._create_novel()
        base = f"/api/v1/novels/{nid}/outline"

        vol = client.post(base, json={"title": "卷", "level": "volume"}).json()
        node = client.post(base, json={"title": "章", "level": "chapter", "parent_id": vol["id"]}).json()

        chapter = client.post(
            f"/api/v1/novels/{nid}/chapters",
            json={"title": "章正文", "content": "正文", "outline_node_id": node["id"]},
        ).json()

        r = client.delete(f"{base}/{node['id']}")
        self.assertEqual(r.status_code, 204)

        # 正文保留，outline_node_id 置空
        kept = client.get(f"/api/v1/novels/{nid}/chapters/{chapter['id']}").json()
        self.assertEqual(kept["content"], "正文")
        self.assertEqual(kept["outline_node_id"], "")


if __name__ == "__main__":
    unittest.main()
