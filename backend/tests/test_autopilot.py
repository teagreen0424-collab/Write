"""阶段三单元测试：auto_pilot 编排 / 熔断 / 幂等恢复 / 张力与漂移。"""
import json
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.application import auto_pilot
from app.infrastructure import models
from app.infrastructure import repository as repos

engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
models.Base.metadata.create_all(bind=engine)
Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def _events(gen):
    out = []
    for line in gen:
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload:
                out.append(json.loads(payload))
    return out


class AutoPilotTest(unittest.TestCase):
    def setUp(self):
        self.db = Session()
        novel = repos.NovelRepo(self.db).create(
            title="测试作品", genre="玄幻", summary="", style_anchor="古风"
        )
        self.nid = novel.id

    def tearDown(self):
        self.db.close()

    def _make_outline(self):
        repo = repos.OutlineRepo(self.db)
        vol = repo.create(self.nid, title="第一卷", level="volume")
        act = repo.create(self.nid, title="第一幕", level="act", parent_id=vol.id)
        return [
            repo.create(self.nid, title="第一章", level="chapter", parent_id=act.id),
            repo.create(self.nid, title="第二章", level="chapter", parent_id=act.id),
            repo.create(self.nid, title="第三章", level="chapter", parent_id=act.id),
        ]

    def test_ordered_chapter_nodes(self):
        chapters = self._make_outline()
        ordered = auto_pilot._ordered_chapter_nodes(self.db, self.nid)
        self.assertEqual([c.id for c in ordered], [c.id for c in chapters])

    def test_score_tension(self):
        with patch.object(auto_pilot.llm, "complete", return_value="7.5"):
            self.assertEqual(auto_pilot.score_tension("正文"), 7.5)

    def test_run_autopilot_flow(self):
        self._make_outline()
        with (
            patch.object(auto_pilot.llm, "stream_complete", side_effect=lambda *args, **kwargs: iter(["这是", "正文"])),
            patch.object(auto_pilot, "score_tension", return_value=6.0),
            patch.object(auto_pilot, "check_drift", return_value=0.8),
        ):
            events = _events(auto_pilot.run_autopilot(self.nid, session_factory=Session))

        types = [e["type"] for e in events]
        self.assertIn("done", types)
        self.assertEqual(types.count("chapter_done"), 3)

        chapters = repos.ChapterRepo(self.db).list(self.nid)
        self.assertEqual(len(chapters), 3)
        self.assertTrue(all(c.tension_score is not None for c in chapters))

    def test_circuit_breaker(self):
        self._make_outline()
        with patch.object(auto_pilot.llm, "stream_complete", side_effect=RuntimeError("boom")):
            events = _events(auto_pilot.run_autopilot(self.nid, session_factory=Session))

        types = [e["type"] for e in events]
        self.assertIn("paused", types)
        # 熔断后没有生成章节
        self.assertEqual(len(repos.ChapterRepo(self.db).list(self.nid)), 0)

    def test_skip_generated(self):
        chapters = self._make_outline()
        repos.ChapterRepo(self.db).create(
            self.nid,
            outline_node_id=chapters[0].id,
            seq=1,
            title="第一章",
            content="已生成",
            status="draft",
        )
        with (
            patch.object(auto_pilot.llm, "stream_complete", side_effect=lambda *args, **kwargs: iter(["新", "内容"])),
            patch.object(auto_pilot, "score_tension", return_value=5.0),
            patch.object(auto_pilot, "check_drift", return_value=None),
        ):
            events = _events(auto_pilot.run_autopilot(self.nid, session_factory=Session))

        self.assertEqual(len(repos.ChapterRepo(self.db).list(self.nid)), 3)
        self.assertTrue(any("跳过" in e.get("message", "") for e in events))


if __name__ == "__main__":
    unittest.main()
