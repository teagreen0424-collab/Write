"""阶段七单元测试：质量曲线与伏笔追踪聚合。"""
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.infrastructure import models
from app.infrastructure import repository as repos
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


class QualityApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 每个测试类运行时重新挂载本模块的 get_db 覆盖，避免被其它测试模块覆盖
        app.dependency_overrides[get_db] = override_get_db

    def setUp(self):
        self.db = Session()
        self.novel = repos.NovelRepo(self.db).create(
            title="测试作品", genre="玄幻", summary="", style_anchor="古风"
        )
        self.nid = self.novel.id

    def tearDown(self):
        self.db.close()

    def _chapter(self, seq, title, content="正文", **extra):
        return repos.ChapterRepo(self.db).create(
            self.nid, seq=seq, title=title, content=content, **extra
        )

    def _fs(self, content, status, **extra):
        return repos.ForeshadowingRepo(self.db).create(
            self.nid, content=content, status=status, **extra
        )

    # ---------- 质量曲线 ----------

    def test_curve_orders_by_seq_and_matches_drift(self):
        # 故意乱序创建，验证按 seq 升序返回
        self._chapter(2, "第二章", status="draft", tension_score=7.0)
        self._chapter(
            1, "第一章", status="finalized", tension_score=6.5, drift_score=0.8
        )

        r = client.get(f"/api/v1/novels/{self.nid}/quality/curve")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()

        self.assertTrue(data["anchor_set"])
        self.assertEqual(data["drift_threshold"], 0.6)
        self.assertEqual([c["seq"] for c in data["chapters"]], [1, 2])
        self.assertEqual(data["chapters"][0]["drift_score"], 0.8)
        self.assertIsNone(data["chapters"][1]["drift_score"])
        self.assertEqual(data["chapters"][0]["status"], "finalized")
        self.assertEqual(data["chapters"][1]["tension_score"], 7.0)

    def test_curve_anchor_not_set(self):
        repos.NovelRepo(self.db).update(self.nid, style_anchor="")
        r = client.get(f"/api/v1/novels/{self.nid}/quality/curve")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["anchor_set"])

    # ---------- 去 AI 味扫描端点 ----------

    def test_cliche_endpoint(self):
        r = client.post(
            f"/api/v1/novels/{self.nid}/quality/cliche", json={"content": "他缓缓开口。"}
        )
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["hits"][0]["term"], "缓缓")
        self.assertEqual(data["hits"][0]["category"], "万能副词")
        self.assertIn("万能副词", data["by_category"])

    # ---------- 伏笔追踪聚合 ----------

    def test_foreshadowing_groups_and_long_pending(self):
        chapters = [self._chapter(seq, f"第{seq}章") for seq in range(1, 13)]
        a = self._fs("身世之谜", "suspended", planted_chapter_id=chapters[0].id)
        b = self._fs("残缺玉佩", "suspended", planted_chapter_id=chapters[7].id)
        c = self._fs("师父旧信", "planted", planted_chapter_id=chapters[4].id)
        d = self._fs(
            "古井封印",
            "consumed",
            planted_chapter_id=chapters[1].id,
            consumed_chapter_id=chapters[5].id,
        )
        e = self._fs("无源伏笔", "suspended")

        r = client.get(f"/api/v1/novels/{self.nid}/quality/foreshadowings")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()

        self.assertEqual(data["latest_seq"], 12)
        self.assertEqual(data["chapter_count"], 12)
        self.assertEqual(data["long_pending_threshold"], {"chapters": 10, "ratio": 0.3})

        groups = data["groups"]
        # 悬置组：长期未闭合优先 → 再按悬置章数降序 → planted_seq 为空的排末尾
        self.assertEqual([x["id"] for x in groups["suspended"]], [a.id, b.id, e.id])
        # 已埋 / 已消费：按 planted_seq 升序
        self.assertEqual([x["id"] for x in groups["planted"]], [c.id])
        self.assertEqual([x["id"] for x in groups["consumed"]], [d.id])

        by_id = {x["id"]: x for x in groups["suspended"]}
        self.assertEqual(by_id[a.id]["planted_seq"], 1)
        self.assertEqual(by_id[a.id]["suspended_chapters"], 11)
        self.assertTrue(by_id[a.id]["long_pending"])
        self.assertEqual(by_id[b.id]["planted_seq"], 8)
        self.assertEqual(by_id[b.id]["suspended_chapters"], 4)
        # 4 >= 0.3 * 12 = 3.6，走比例分支
        self.assertTrue(by_id[b.id]["long_pending"])
        self.assertIsNone(by_id[e.id]["planted_seq"])
        self.assertIsNone(by_id[e.id]["suspended_chapters"])
        self.assertFalse(by_id[e.id]["long_pending"])

        planted = groups["planted"][0]
        self.assertEqual(planted["planted_seq"], 5)
        self.assertEqual(planted["suspended_chapters"], 7)
        self.assertFalse(planted["long_pending"])

        consumed = groups["consumed"][0]
        self.assertEqual(consumed["planted_seq"], 2)
        # 已消费：consumed_seq - planted_seq - 1 = 6 - 2 - 1
        self.assertEqual(consumed["suspended_chapters"], 3)

    def test_long_pending_threshold_takes_earlier_branch(self):
        chapters = [self._chapter(seq, f"第{seq}章") for seq in range(1, 41)]
        by_chapters = self._fs(
            "十章的悬置", "suspended", planted_chapter_id=chapters[0].id
        )
        by_ratio_fail = self._fs(
            "九章的悬置", "suspended", planted_chapter_id=chapters[30].id
        )

        r = client.get(f"/api/v1/novels/{self.nid}/quality/foreshadowings")
        self.assertEqual(r.status_code, 200, r.text)
        groups = r.json()["groups"]["suspended"]
        by_id = {x["id"]: x for x in groups}
        # 39 章悬置 → 命中「≥10 章」
        self.assertEqual(by_id[by_chapters.id]["suspended_chapters"], 39)
        self.assertTrue(by_id[by_chapters.id]["long_pending"])
        # 9 章悬置，比例分支为 0.3 * 40 = 12，两个条件都不满足
        self.assertEqual(by_id[by_ratio_fail.id]["suspended_chapters"], 9)
        self.assertFalse(by_id[by_ratio_fail.id]["long_pending"])

    def test_chapter_count_counts_written_only(self):
        chapters = [self._chapter(seq, f"第{seq}章") for seq in range(1, 6)]
        # 空正文的章节不计入「已写章节」
        self._chapter(6, "第六章", content="")
        self._fs("悬置伏笔", "suspended", planted_chapter_id=chapters[0].id)

        r = client.get(f"/api/v1/novels/{self.nid}/quality/foreshadowings")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["chapter_count"], 5)
        self.assertEqual(data["latest_seq"], 5)
        self.assertEqual(data["groups"]["suspended"][0]["suspended_chapters"], 4)


if __name__ == "__main__":
    unittest.main()
