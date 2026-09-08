"""单元测试：删除作品时级联清理 8 张子表，避免孤儿数据残留。"""
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


def _count(model, novel_id: str) -> int:
    db = Session()
    try:
        return db.query(model).filter(model.novel_id == novel_id).count()
    finally:
        db.close()


CHILD_MODELS = (
    models.Character,
    models.Location,
    models.WorldSetting,
    models.TimelineEvent,
    models.Foreshadowing,
    models.Relationship,
    models.Chapter,
    models.OutlineNode,
)


class NovelDeleteCascadeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 每个测试类运行时重新挂载本模块的 get_db 覆盖，避免被其它测试模块覆盖
        app.dependency_overrides[get_db] = override_get_db

    def test_delete_novel_cascades_children(self):
        novel = client.post("/api/v1/novels", json={"title": "测试作品", "genre": "玄幻"}).json()
        nid = novel["id"]

        client.post(f"/api/v1/novels/{nid}/characters", json={"name": "林越"})
        client.post(f"/api/v1/novels/{nid}/chapters", json={"title": "第一章", "content": "正文"})
        client.post(f"/api/v1/novels/{nid}/outline", json={"title": "第一卷", "level": "volume"})

        # 删除前关键子表各 1 条
        self.assertEqual(_count(models.Character, nid), 1)
        self.assertEqual(_count(models.Chapter, nid), 1)
        self.assertEqual(_count(models.OutlineNode, nid), 1)

        resp = client.delete(f"/api/v1/novels/{nid}")
        self.assertEqual(resp.status_code, 204)

        # 8 张子表均无该 novel_id 残留
        for model in CHILD_MODELS:
            self.assertEqual(_count(model, nid), 0)

        # 作品本身已删除，GET 返回 404
        self.assertEqual(client.get(f"/api/v1/novels/{nid}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
