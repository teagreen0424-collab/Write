"""阶段二单元测试：build_context / settle_chapter / consistency_check。"""
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.application import consistency as consistency_uc
from app.application import context as context_uc
from app.application import settle as settle_uc
from app.application import writing as writing_uc
from app.infrastructure import models
from app.infrastructure import repository as repos
from app.main import app

engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
models.Base.metadata.create_all(bind=engine)
Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
client = TestClient(app)


def _parse_sse(stream) -> list[dict]:
    """把 SSE 文本行解析为事件字典。"""
    events = []
    for line in stream:
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: ") :]))
    return events


class WritingUseCaseTest(unittest.TestCase):
    def setUp(self):
        self.db = Session()
        novel = repos.NovelRepo(self.db).create(title="测试作品", genre="玄幻", summary="")
        self.novel_id = novel.id

    def tearDown(self):
        self.db.close()

    def _add_character(self, name, known_info=""):
        return repos.CharacterRepo(self.db).create(self.novel_id, name=name, known_info=known_info)

    def test_build_context_priority(self):
        self._add_character("林越", known_info="青云宗弟子")
        repos.ChapterRepo(self.db).create(
            self.novel_id, seq=1, title="第一章", content="...", status="finalized", summary="林越入门"
        )
        text = context_uc.build_context(
            self.db, self.novel_id, directive="让林越与苏婉儿决裂", chapter_position="第 2 章"
        )
        self.assertIn("让林越与苏婉儿决裂", text)
        self.assertIn("林越", text)
        self.assertIn("林越入门", text)

    def test_consistency_foreshadowing(self):
        repos.ForeshadowingRepo(self.db).create(
            self.novel_id, content="身世之谜", status="consumed"
        )
        result = consistency_uc.consistency_check(self.db, self.novel_id, "他忽然想起身世之谜")
        self.assertTrue(any(c["rule"] == "foreshadowing" for c in result["conflicts"]))

    def test_consistency_known_info(self):
        self._add_character("林越", known_info="不知道自己是青云宗遗孤")
        result = consistency_uc.consistency_check(self.db, self.novel_id, "林越得知自己是青云宗遗孤")
        self.assertTrue(any(c["rule"] == "known_info" for c in result["conflicts"]))

    def test_settle_chapter(self):
        chapter = repos.ChapterRepo(self.db).create(
            self.novel_id, seq=1, title="第一章", content="林越在青云山下捡到一枚玉佩。"
        )
        fake = (
            '{"summary":"林越捡到玉佩",'
            '"characters":[{"name":"苏婉儿","role":"配角"}],'
            '"events":[],'
            '"foreshadowings":[{"content":"玉佩来历","status":"planted"}]}'
        )
        with (
            patch("app.application.settle.llm.complete", return_value=fake),
            patch("app.application.settle.write_chapter_memory", return_value=0),
        ):
            result = settle_uc.settle_chapter(self.db, self.novel_id, chapter.id)

        self.assertEqual(result["summary"], "林越捡到玉佩")
        self.assertEqual(len(result["suggestions"]), 2)
        updated = repos.ChapterRepo(self.db).get(self.novel_id, chapter.id)
        self.assertEqual(updated.status, "finalized")

    def test_apply_suggestions(self):
        suggestions = [
            {"type": "character", "data": {"name": "苏婉儿", "role": "配角"}},
            {"type": "foreshadowing", "data": {"content": "玉佩来历", "status": "planted"}},
        ]
        result = settle_uc.apply_suggestions(self.db, self.novel_id, suggestions)
        self.assertEqual(len(result["created"]), 2)
        self.assertEqual(len(repos.CharacterRepo(self.db).list(self.novel_id)), 1)
        self.assertEqual(len(repos.ForeshadowingRepo(self.db).list(self.novel_id)), 1)

    def test_rewrite_preview_not_persisted(self):
        chapter = repos.ChapterRepo(self.db).create(
            self.novel_id, seq=1, title="第一章", content="开头。原文段落内容。结尾。"
        )
        with patch(
            "app.application.writing.llm.stream_complete", return_value=iter(["改写后"])
        ):
            events = _parse_sse(
                writing_uc.rewrite_passage(
                    self.db, self.novel_id, chapter.id, "原文段落内容", preview=True
                )
            )

        self.assertFalse(any(e["type"] == "error" for e in events))
        done = next(e for e in events if e["type"] == "done")
        self.assertTrue(done["preview"])
        self.assertEqual(done["content"], "开头。改写后。结尾。")
        # 预览不落库：正文保持原文
        self.assertEqual(
            repos.ChapterRepo(self.db).get(self.novel_id, chapter.id).content,
            "开头。原文段落内容。结尾。",
        )

    def test_rewrite_default_persists(self):
        chapter = repos.ChapterRepo(self.db).create(
            self.novel_id, seq=1, title="第一章", content="开头。原文段落内容。结尾。"
        )
        with patch(
            "app.application.writing.llm.stream_complete", return_value=iter(["改写后"])
        ):
            events = _parse_sse(
                writing_uc.rewrite_passage(
                    self.db, self.novel_id, chapter.id, "原文段落内容"
                )
            )

        done = next(e for e in events if e["type"] == "done")
        self.assertNotIn("preview", done)
        self.assertEqual(
            repos.ChapterRepo(self.db).get(self.novel_id, chapter.id).content,
            "开头。改写后。结尾。",
        )

    def test_write_route_persists_outline_node_id(self):
        volume = repos.OutlineRepo(self.db).create(
            self.novel_id, title="第一卷", level="volume"
        )
        outline = repos.OutlineRepo(self.db).create(
            self.novel_id, title="第一章", level="chapter",
            parent_id=volume.id, summary="本章必须出现的剧情要点",
        )
        with (
            patch("app.interfaces.api.v1.chapters.SessionLocal", side_effect=Session),
            patch("app.application.writing.llm.stream_complete", return_value=iter(["正文"])) as stream,
        ):
            response = client.post(
                f"/api/v1/novels/{self.novel_id}/chapters/write",
                json={"title": "第一章", "outline_node_id": outline.id},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('"type": "done"', response.text)
        chapters = repos.ChapterRepo(self.db).list(self.novel_id)
        self.assertEqual(len(chapters), 1)
        self.assertEqual(chapters[0].outline_node_id, outline.id)
        self.assertIn("本章必须出现的剧情要点", stream.call_args.args[0][1]["content"])

    def test_write_route_rejects_non_chapter_outline_node(self):
        volume = repos.OutlineRepo(self.db).create(
            self.novel_id, title="第一卷", level="volume"
        )
        with (
            patch("app.interfaces.api.v1.chapters.SessionLocal", side_effect=Session),
            patch("app.application.writing.llm.stream_complete") as stream,
        ):
            response = client.post(
                f"/api/v1/novels/{self.novel_id}/chapters/write",
                json={"outline_node_id": volume.id},
            )
        self.assertIn("只能绑定章级大纲节点", response.text)
        stream.assert_not_called()
        self.assertEqual(repos.ChapterRepo(self.db).list(self.novel_id), [])

    def test_write_route_without_outline_node_id_keeps_empty_value(self):
        with (
            patch("app.interfaces.api.v1.chapters.SessionLocal", side_effect=Session),
            patch("app.application.writing.llm.stream_complete", return_value=iter(["正文"])),
        ):
            response = client.post(
                f"/api/v1/novels/{self.novel_id}/chapters/write",
                json={"title": "第一章"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        chapters = repos.ChapterRepo(self.db).list(self.novel_id)
        self.assertEqual(len(chapters), 1)
        self.assertEqual(chapters[0].outline_node_id, "")

    def test_write_next_chapter_includes_continuity_context(self):
        volume = repos.OutlineRepo(self.db).create(
            self.novel_id, title="第一卷", level="volume"
        )
        outline = repos.OutlineRepo(self.db).create(
            self.novel_id, title="第二章", level="chapter", parent_id=volume.id,
            summary="落地后有消毒水混着铁锈的气味",
        )
        previous = repos.ChapterRepo(self.db).create(
            self.novel_id,
            seq=1,
            title="雨夜相遇",
            content="章首身份：林越是外卖员。" + "中段经历" * 1000 + "上一章结尾线索",
            status="draft",
        )
        with (
            patch("app.interfaces.api.v1.chapters.SessionLocal", side_effect=Session),
            patch(
                "app.application.writing.llm.stream_complete",
                return_value=iter(["承接后的第二章正文"]),
            ) as stream,
        ):
            response = client.post(
                f"/api/v1/novels/{self.novel_id}/chapters/write",
                json={"previous_chapter_id": previous.id, "outline_node_id": outline.id},
            )

        self.assertEqual(response.status_code, 200, response.text)
        messages = stream.call_args.args[0]
        self.assertIn("必须把给出的上一章视为已经发生", messages[0]["content"])
        self.assertIn("【必须直接承接的上一章】", messages[1]["content"])
        self.assertIn("章首身份：林越是外卖员", messages[1]["content"])
        self.assertIn("上一章结尾线索", messages[1]["content"])
        self.assertIn("不得再次写成第一次", messages[1]["content"])
        self.assertIn("未沉淀，以正文为准", messages[1]["content"])
        self.assertIn("落地后有消毒水混着铁锈的气味", messages[1]["content"])
        self.assertIn("章级大纲的具体场面与上一章已发生事实冲突", messages[0]["content"])
        chapters = repos.ChapterRepo(self.db).list(self.novel_id)
        self.assertEqual(len(chapters), 2)
        self.assertEqual(chapters[1].seq, 2)
        self.assertEqual(chapters[1].title, "第2章")
        self.assertEqual(chapters[1].content, "承接后的第二章正文")
        self.assertEqual(chapters[1].outline_node_id, outline.id)

    def test_write_next_chapter_rejects_non_latest_previous_chapter(self):
        first = repos.ChapterRepo(self.db).create(
            self.novel_id, seq=1, title="第一章", content="第一章正文"
        )
        repos.ChapterRepo(self.db).create(
            self.novel_id, seq=2, title="第二章", content="第二章正文"
        )
        with (
            patch("app.interfaces.api.v1.chapters.SessionLocal", side_effect=Session),
            patch("app.application.writing.llm.stream_complete") as stream,
        ):
            response = client.post(
                f"/api/v1/novels/{self.novel_id}/chapters/write",
                json={"previous_chapter_id": first.id},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("只能从最新章节生成下一章", response.text)
        stream.assert_not_called()
        self.assertEqual(len(repos.ChapterRepo(self.db).list(self.novel_id)), 2)

    def test_delete_chapter_cleans_memory_and_foreshadowing_references(self):
        chapter = repos.ChapterRepo(self.db).create(
            self.novel_id, seq=1, title="第一章", content="将被删除的正文"
        )
        repos.MemoryChunkRepo(self.db).create(
            self.novel_id,
            kind="chapter_slice",
            source_chapter_id=chapter.id,
            text="派生记忆",
            embedding_id="",
            vector=[],
        )
        foreshadowing = repos.ForeshadowingRepo(self.db).create(
            self.novel_id,
            content="仍需保留的伏笔",
            planted_chapter_id=chapter.id,
            consumed_chapter_id=chapter.id,
        )

        repos.ChapterRepo(self.db).delete(self.novel_id, chapter.id)

        self.assertEqual(repos.ChapterRepo(self.db).list(self.novel_id), [])
        memories = repos.MemoryChunkRepo(self.db).list_by_novel(self.novel_id)
        self.assertFalse(any(item.source_chapter_id == chapter.id for item in memories))
        kept = repos.ForeshadowingRepo(self.db).get(self.novel_id, foreshadowing.id)
        self.assertEqual(kept.planted_chapter_id, "")
        self.assertEqual(kept.consumed_chapter_id, "")

    def test_settle_with_anchor_calls_extraction_and_drift_once(self):
        repos.NovelRepo(self.db).update(self.novel_id, style_anchor="简练古风")
        chapter = repos.ChapterRepo(self.db).create(
            self.novel_id, seq=1, title="第一章", content="林越在青云山下捡到一枚玉佩。"
        )
        fake = (
            '{"summary":"林越捡到玉佩","characters":[],'
            '"events":[],"foreshadowings":[]}'
        )
        with (
            patch("app.application.settle.llm.complete", return_value=fake) as extraction,
            patch("app.application.settle.write_chapter_memory", return_value=0),
            patch("app.application.settle.check_drift", return_value=0.42) as drift,
            patch("app.application.consistency.deep_check") as deep_check,
        ):
            result = settle_uc.settle_chapter(self.db, self.novel_id, chapter.id)

        extraction.assert_called_once()
        drift.assert_called_once_with(self.db, self.novel_id, chapter.content)
        deep_check.assert_not_called()
        self.assertEqual(result["drift"], 0.42)
        self.assertEqual(
            repos.ChapterRepo(self.db).get(self.novel_id, chapter.id).drift_score, 0.42
        )

    def test_settle_without_anchor_skips_drift_and_deep_check(self):
        chapter = repos.ChapterRepo(self.db).create(
            self.novel_id, seq=1, title="第一章", content="林越走进青云山。"
        )
        fake = (
            '{"summary":"林越进山","characters":[],'
            '"events":[],"foreshadowings":[]}'
        )
        with (
            patch("app.application.settle.llm.complete", return_value=fake) as extraction,
            patch("app.application.settle.write_chapter_memory", return_value=0),
            patch("app.application.settle.check_drift") as drift,
            patch("app.application.consistency.deep_check") as deep_check,
        ):
            result = settle_uc.settle_chapter(self.db, self.novel_id, chapter.id)

        extraction.assert_called_once()
        drift.assert_not_called()
        deep_check.assert_not_called()
        self.assertIsNone(result["drift"])


if __name__ == "__main__":
    unittest.main()
