"""阶段八第一轮单元测试：章节版本快照、原子恢复与派生数据失效。

测试全部落在真实 SQLite 结果上（内存库 + StaticPool），只对 LLM 调用打桩。
"""
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.application import auto_pilot
from app.application import revisions as revisions_uc
from app.application import writing as writing_uc
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


def _parse_sse(stream) -> list[dict]:
    """把 SSE 文本行解析为事件字典。"""
    events = []
    for line in stream:
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: ") :]))
    return events


class RevisionTestBase(unittest.TestCase):
    """所有用例各自建一个作品，避免共享内存库时互相影响。"""

    @classmethod
    def setUpClass(cls):
        # 多个测试模块都会覆盖 get_db，运行时重新挂载本模块的覆盖，避免被其它模块抢走。
        app.dependency_overrides[get_db] = override_get_db

    def setUp(self):
        self.db = Session()
        novel = repos.NovelRepo(self.db).create(title="测试作品", genre="玄幻")
        self.novel_id = novel.id

    def tearDown(self):
        self.db.close()

    # ---------- 工具 ----------
    def _new_novel(self, title="另一作品"):
        return repos.NovelRepo(self.db).create(title=title, genre="都市").id

    def _chapter(self, title="第一章", content="开头。原文段落内容。结尾。", seq=1, **extra):
        return repos.ChapterRepo(self.db).create(
            self.novel_id, seq=seq, title=title, content=content, **extra
        )

    def _fresh_chapter(self, chapter_id):
        self.db.expire_all()
        return repos.ChapterRepo(self.db).get(self.novel_id, chapter_id)

    def _revision_count(self, chapter_id):
        self.db.expire_all()
        return (
            self.db.query(models.ChapterRevision)
            .filter(
                models.ChapterRevision.novel_id == self.novel_id,
                models.ChapterRevision.chapter_id == chapter_id,
            )
            .count()
        )

    def _revisions(self, chapter_id):
        """按版本号升序返回版本，方便按下标断言。"""
        self.db.expire_all()
        return (
            self.db.query(models.ChapterRevision)
            .filter(
                models.ChapterRevision.novel_id == self.novel_id,
                models.ChapterRevision.chapter_id == chapter_id,
            )
            .order_by(models.ChapterRevision.revision_no)
            .all()
        )

    def _add_memory(self, chapter_id, text="派生记忆"):
        return repos.MemoryChunkRepo(self.db).create(
            self.novel_id,
            kind="chapter_slice",
            source_chapter_id=chapter_id,
            text=text,
            embedding_id="",
            vector=[],
        )

    def _memory_count(self, chapter_id):
        self.db.expire_all()
        return (
            self.db.query(models.MemoryChunk)
            .filter(
                models.MemoryChunk.novel_id == self.novel_id,
                models.MemoryChunk.source_chapter_id == chapter_id,
            )
            .count()
        )


def _parse_sse_auto(gen) -> list[dict]:
    """自动驾驶生成器产出的是 data: 行文本。"""
    out = []
    for line in gen:
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload:
                out.append(json.loads(payload))
    return out


# ================= 1. 快照触发条件 =================
class SnapshotTriggerTest(RevisionTestBase):
    def test_word_count_uses_unicode_characters_and_python_whitespace(self):
        self.assertEqual(revisions_uc.count_words("吴迪😀𠀀\u0085\u001c下一章"), 7)

    def test_manual_content_change_saves_previous_version(self):
        chapter = self._chapter()

        updated = revisions_uc.update_chapter_with_revision(
            self.db,
            self.novel_id,
            chapter.id,
            {"content": "改写后的正文"},
            revisions_uc.SOURCE_MANUAL_SAVE,
        )

        self.assertEqual(updated.content, "改写后的正文")
        revisions = self._revisions(chapter.id)
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0].revision_no, 1)
        self.assertEqual(revisions[0].source, "manual_save")
        self.assertEqual(revisions[0].content, "开头。原文段落内容。结尾。")
        self.assertEqual(revisions[0].title, "第一章")
        self.assertEqual(revisions[0].word_count, revisions_uc.count_words("开头。原文段落内容。结尾。"))

    def test_title_change_saves_version(self):
        chapter = self._chapter()

        revisions_uc.update_chapter_with_revision(
            self.db, self.novel_id, chapter.id, {"title": "新标题"}, revisions_uc.SOURCE_MANUAL_SAVE
        )

        revisions = self._revisions(chapter.id)
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0].title, "第一章")

    def test_snapshot_failure_rolls_back_chapter_update(self):
        """快照失败必须连带回滚正文：不许出现「正文更新了但没留版本」。"""
        chapter = self._chapter()

        with patch.object(
            repos.ChapterRevisionRepo, "add_snapshot", side_effect=RuntimeError("快照写入失败")
        ):
            with self.assertRaises(RuntimeError):
                revisions_uc.update_chapter_with_revision(
                    self.db,
                    self.novel_id,
                    chapter.id,
                    {"content": "这份正文不该落库"},
                    revisions_uc.SOURCE_MANUAL_SAVE,
                )

        self.assertEqual(self._fresh_chapter(chapter.id).content, "开头。原文段落内容。结尾。")
        self.assertEqual(self._revision_count(chapter.id), 0)

    def test_directive_change_saves_version(self):
        chapter = self._chapter()
        revisions_uc.update_chapter_with_revision(
            self.db,
            self.novel_id,
            chapter.id,
            {"directive": "让两人决裂"},
            revisions_uc.SOURCE_MANUAL_SAVE,
        )

        revisions = self._revisions(chapter.id)
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0].directive, "")

    def test_identical_content_save_creates_no_version(self):
        chapter = self._chapter()
        same = {"title": "第一章", "content": "开头。原文段落内容。结尾。", "directive": ""}

        revisions_uc.update_chapter_with_revision(
            self.db, self.novel_id, chapter.id, same, revisions_uc.SOURCE_MANUAL_SAVE
        )

        self.assertEqual(self._revision_count(chapter.id), 0)

    def test_non_author_field_change_creates_no_version(self):
        chapter = self._chapter()

        revisions_uc.update_chapter_with_revision(
            self.db,
            self.novel_id,
            chapter.id,
            {"status": "finalized", "summary": "摘要", "outline_node_id": "node-1"},
            revisions_uc.SOURCE_MANUAL_SAVE,
        )

        self.assertEqual(self._revision_count(chapter.id), 0)
        self.assertEqual(self._fresh_chapter(chapter.id).status, "finalized")

    def test_snapshot_keeps_review_fields_and_word_count(self):
        chapter = self._chapter(content="林越  走进\n青云山。")
        repos.ChapterRepo(self.db).update(
            self.novel_id,
            chapter.id,
            status="finalized",
            summary="林越进山",
            tension_score=6.5,
            drift_score=0.25,
        )

        revisions_uc.update_chapter_with_revision(
            self.db, self.novel_id, chapter.id, {"content": "换成新正文"}, revisions_uc.SOURCE_MANUAL_SAVE
        )

        snapshot = self._revisions(chapter.id)[0]
        self.assertEqual(snapshot.status, "finalized")
        self.assertEqual(snapshot.summary, "林越进山")
        self.assertEqual(snapshot.tension_score, 6.5)
        self.assertEqual(snapshot.drift_score, 0.25)
        # 字数口径 = 去除所有空白字符后的字符数
        self.assertEqual(snapshot.word_count, len("林越走进青云山。"))


# ================= 2. 写入路径接入 =================
class WritePathRevisionTest(RevisionTestBase):
    def test_continue_saves_previous_version_and_appends(self):
        chapter = self._chapter()

        with patch("app.application.writing.llm.stream_complete", return_value=iter(["新续写段落"])):
            events = _parse_sse(
                writing_uc.continue_chapter(self.db, self.novel_id, chapter.id, "继续推进")
            )

        self.assertFalse(any(e["type"] == "error" for e in events))
        self.assertEqual(self._fresh_chapter(chapter.id).content, "开头。原文段落内容。结尾。新续写段落")
        revisions = self._revisions(chapter.id)
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0].source, "continue")
        self.assertEqual(revisions[0].content, "开头。原文段落内容。结尾。")

    def test_direct_rewrite_saves_previous_version(self):
        chapter = self._chapter()

        with patch("app.application.writing.llm.stream_complete", return_value=iter(["改写后"])):
            events = _parse_sse(
                writing_uc.rewrite_passage(self.db, self.novel_id, chapter.id, "原文段落内容")
            )

        self.assertFalse(any(e["type"] == "error" for e in events))
        self.assertEqual(self._fresh_chapter(chapter.id).content, "开头。改写后。结尾。")
        revisions = self._revisions(chapter.id)
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0].source, "rewrite")
        self.assertEqual(revisions[0].content, "开头。原文段落内容。结尾。")

    def test_rewrite_preview_creates_no_version_and_keeps_content(self):
        chapter = self._chapter()

        with patch("app.application.writing.llm.stream_complete", return_value=iter(["改写后"])):
            events = _parse_sse(
                writing_uc.rewrite_passage(
                    self.db, self.novel_id, chapter.id, "原文段落内容", preview=True
                )
            )

        done = next(e for e in events if e["type"] == "done")
        self.assertTrue(done["preview"])
        self.assertEqual(self._fresh_chapter(chapter.id).content, "开头。原文段落内容。结尾。")
        self.assertEqual(self._revision_count(chapter.id), 0)

    def test_put_default_source_is_manual_save(self):
        chapter = self._chapter()

        resp = client.put(
            f"/api/v1/novels/{self.novel_id}/chapters/{chapter.id}",
            json={"content": "手工改过的正文"},
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["content"], "手工改过的正文")
        revisions = self._revisions(chapter.id)
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0].source, "manual_save")
        self.assertEqual(revisions[0].content, "开头。原文段落内容。结尾。")

    def test_put_revision_source_rewrite_apply(self):
        chapter = self._chapter()

        resp = client.put(
            f"/api/v1/novels/{self.novel_id}/chapters/{chapter.id}",
            json={"content": "改写应用后的正文", "revision_source": "rewrite_apply"},
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        revisions = self._revisions(chapter.id)
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0].source, "rewrite_apply")

    def test_put_rejects_unknown_revision_source(self):
        chapter = self._chapter()

        resp = client.put(
            f"/api/v1/novels/{self.novel_id}/chapters/{chapter.id}",
            json={"content": "不该落库", "revision_source": "restore"},
        )

        self.assertEqual(resp.status_code, 422)
        self.assertEqual(self._fresh_chapter(chapter.id).content, "开头。原文段落内容。结尾。")
        self.assertEqual(self._revision_count(chapter.id), 0)

    def test_put_without_author_change_creates_no_version(self):
        chapter = self._chapter()

        resp = client.put(
            f"/api/v1/novels/{self.novel_id}/chapters/{chapter.id}",
            json={"title": "第一章", "status": "finalized", "revision_source": "manual_save"},
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._revision_count(chapter.id), 0)

    def test_put_clearing_author_field_saves_version(self):
        chapter = self._chapter(directive="让两人决裂")

        resp = client.put(
            f"/api/v1/novels/{self.novel_id}/chapters/{chapter.id}",
            json={"directive": "", "revision_source": "manual_save"},
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["directive"], "")
        revisions = self._revisions(chapter.id)
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0].directive, "让两人决裂")

    def test_revision_source_is_not_persisted_as_chapter_field(self):
        chapter = self._chapter()

        resp = client.put(
            f"/api/v1/novels/{self.novel_id}/chapters/{chapter.id}",
            json={"content": "新的正文", "revision_source": "rewrite_apply"},
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertNotIn("revision_source", body)
        self.assertFalse(hasattr(self._fresh_chapter(chapter.id), "revision_source"))

    def test_create_chapter_produces_no_version(self):
        resp = client.post(
            f"/api/v1/novels/{self.novel_id}/chapters", json={"title": "新章", "content": "正文"}
        )

        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(self._revision_count(resp.json()["id"]), 0)

    def test_autopilot_new_chapter_produces_no_version(self):
        repo = repos.OutlineRepo(self.db)
        volume = repo.create(self.novel_id, title="第一卷", level="volume")
        node = repo.create(self.novel_id, title="第一章", level="chapter", parent_id=volume.id)

        with (
            patch.object(auto_pilot.llm, "stream_complete", return_value=iter(["自动驾驶正文"])),
            patch.object(auto_pilot, "score_tension", return_value=5.0),
            patch.object(auto_pilot, "check_drift", return_value=None),
        ):
            events = _parse_sse_auto(auto_pilot.run_autopilot(self.novel_id, session_factory=Session))

        self.assertTrue(any(e["type"] == "chapter_done" for e in events))
        chapters = repos.ChapterRepo(self.db).list(self.novel_id)
        self.assertEqual(len(chapters), 1)
        self.assertEqual(chapters[0].outline_node_id, node.id)
        self.assertEqual(self._revision_count(chapters[0].id), 0)


# ================= 摘要独立保存与续章上下文 =================
class SummarySaveContractTest(RevisionTestBase):
    def _persisted_rows(self, summary_chapter_id=None):
        """摘要更新只允许目标章的摘要、更新时间变化，其余持久化内容逐字段比较。"""
        self.db.expire_all()
        stored = {}
        for table in models.Base.metadata.sorted_tables:
            rows = []
            for row in self.db.execute(select(table)).mappings():
                values = dict(row)
                if table.name == "chapter" and values["id"] == summary_chapter_id:
                    values.pop("summary")
                    values.pop("updated_at")
                rows.append(values)
            stored[table.name] = sorted(rows, key=lambda row: row["id"])
        return stored

    def test_put_summary_only_preserves_other_persisted_fields_and_can_clear(self):
        outline = repos.OutlineRepo(self.db).create(
            self.novel_id, title="再试一次", level="chapter", summary="尝试瞬移"
        )
        repos.CharacterRepo(self.db).create(self.novel_id, name="吴迪", known_info="能力原因未明")
        for seq, status in enumerate(("draft", "finalized"), start=1):
            with self.subTest(status=status):
                chapter = self._chapter(
                    seq=seq, status=status, summary="旧摘要", directive="保留原因未明",
                    outline_node_id=outline.id, tension_score=6.5, drift_score=0.2,
                )
                revisions_uc.update_chapter_with_revision(
                    self.db, self.novel_id, chapter.id,
                    {"content": "近距离成功，四米尝试未成，原因未明。"},
                    revisions_uc.SOURCE_MANUAL_SAVE,
                )
                self._add_memory(chapter.id, text="正文记忆不随摘要校正改写")
                repos.ForeshadowingRepo(self.db).create(
                    self.novel_id, content="触发原因未明", planted_chapter_id=chapter.id
                )
                before = self._persisted_rows(chapter.id)
                endpoint = f"/api/v1/novels/{self.novel_id}/chapters/{chapter.id}"

                with (
                    patch("app.infrastructure.ai.llm_provider.LLMProvider.complete") as complete,
                    patch("app.infrastructure.ai.llm_provider.LLMProvider.stream_complete") as stream,
                    patch("app.interfaces.api.v1.chapters.settle_uc.settle_chapter") as settle,
                ):
                    for summary in ("四米的几次尝试未成，原因未明。", ""):
                        response = client.put(endpoint, json={"summary": summary})

                        self.assertEqual(response.status_code, 200, response.text)
                        saved = client.get(endpoint)
                        self.assertEqual(saved.status_code, 200, saved.text)
                        self.assertEqual(saved.json()["summary"], summary)
                        self.assertEqual(self._persisted_rows(chapter.id), before)
                    complete.assert_not_called()
                    stream.assert_not_called()
                    settle.assert_not_called()

    def test_put_summary_cannot_write_chapter_from_another_novel(self):
        chapter = self._chapter(summary="原作品摘要")
        other_novel_id = self._new_novel()
        before = self._persisted_rows()

        response = client.put(
            f"/api/v1/novels/{other_novel_id}/chapters/{chapter.id}",
            json={"summary": "不该写入的摘要"},
        )

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(self._persisted_rows(), before)

    def test_api_saved_summaries_are_sent_to_model_when_writing_third_chapter(self):
        first = self._chapter(status="finalized", summary="第一章旧摘要已作废")
        second = self._chapter(
            seq=2, title="再试一次", content="吴迪应下赵鹏介绍的工作。",
            status="draft", summary="第二章旧摘要已作废",
        )
        summaries = {
            first.id: "吴迪失衡时意外瞬移，后来困在树枝上。",
            second.id: "吴迪回屋，四米尝试未成，原因未明，并应下赵鹏介绍的工作。",
        }
        for chapter_id, summary in summaries.items():
            saved = client.put(
                f"/api/v1/novels/{self.novel_id}/chapters/{chapter_id}",
                json={"summary": summary},
            )
            self.assertEqual(saved.status_code, 200, saved.text)

        with (
            patch("app.interfaces.api.v1.chapters.SessionLocal", side_effect=Session),
            patch("app.application.context._semantic_recall", return_value=""),
            patch(
                "app.application.writing.llm.stream_complete",
                return_value=iter(["吴迪去做第一单跑腿。"]),
            ) as stream,
        ):
            response = client.post(
                f"/api/v1/novels/{self.novel_id}/chapters/write",
                json={"previous_chapter_id": second.id, "title": "跑腿第一单"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('"type": "done"', response.text)
        stream.assert_called_once()
        prompt = stream.call_args.args[0][1]["content"]
        for summary in summaries.values():
            self.assertIn(summary, prompt)
        self.assertNotIn("第一章旧摘要已作废", prompt)
        self.assertNotIn("第二章旧摘要已作废", prompt)
        self.assertIn("吴迪应下赵鹏介绍的工作。", prompt)
        chapters = repos.ChapterRepo(self.db).list(self.novel_id)
        self.assertEqual(len(chapters), 3)
        self.assertEqual(chapters[-1].content, "吴迪去做第一单跑腿。")
        self.assertEqual(chapters[-1].status, "draft")
        self.assertEqual(self._revision_count(first.id), 0)
        self.assertEqual(self._revision_count(second.id), 0)


# ================= 3. 版本 API =================
class RevisionApiTest(RevisionTestBase):
    def _make_revisions(self, chapter_id):
        revisions_uc.update_chapter_with_revision(
            self.db, self.novel_id, chapter_id, {"content": "第二版正文"}, revisions_uc.SOURCE_MANUAL_SAVE
        )
        revisions_uc.update_chapter_with_revision(
            self.db, self.novel_id, chapter_id, {"content": "第三版正文"}, revisions_uc.SOURCE_REWRITE
        )
        return self._revisions(chapter_id)

    def test_list_returns_meta_without_content_and_descending(self):
        chapter = self._chapter()
        self._make_revisions(chapter.id)

        resp = client.get(f"/api/v1/novels/{self.novel_id}/chapters/{chapter.id}/revisions")

        self.assertEqual(resp.status_code, 200, resp.text)
        items = resp.json()
        self.assertEqual([item["revision_no"] for item in items], [2, 1])
        self.assertEqual(items[0]["source"], "rewrite")
        self.assertEqual(items[1]["source"], "manual_save")
        for item in items:
            self.assertNotIn("content", item)
            self.assertIn("word_count", item)
            self.assertIn("created_at", item)

    def test_detail_returns_content(self):
        chapter = self._chapter()
        revisions = self._make_revisions(chapter.id)
        first = revisions[0]

        resp = client.get(
            f"/api/v1/novels/{self.novel_id}/chapters/{chapter.id}/revisions/{first.id}"
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["revision_no"], 1)
        self.assertEqual(body["content"], "开头。原文段落内容。结尾。")
        self.assertEqual(body["word_count"], revisions_uc.count_words(body["content"]))

    def test_cross_novel_access_rejected(self):
        chapter = self._chapter()
        revision = self._make_revisions(chapter.id)[0]
        other = self._new_novel()

        list_resp = client.get(f"/api/v1/novels/{other}/chapters/{chapter.id}/revisions")
        detail_resp = client.get(
            f"/api/v1/novels/{other}/chapters/{chapter.id}/revisions/{revision.id}"
        )
        restore_resp = client.post(
            f"/api/v1/novels/{other}/chapters/{chapter.id}/revisions/{revision.id}/restore"
        )

        self.assertEqual(list_resp.status_code, 404)
        self.assertEqual(detail_resp.status_code, 404)
        self.assertEqual(restore_resp.status_code, 404)
        self.assertIn("detail", detail_resp.json())

    def test_cross_chapter_access_rejected(self):
        chapter = self._chapter()
        revision = self._make_revisions(chapter.id)[0]
        other_chapter = self._chapter(title="第二章", content="第二章正文", seq=2)

        detail_resp = client.get(
            f"/api/v1/novels/{self.novel_id}/chapters/{other_chapter.id}/revisions/{revision.id}"
        )
        restore_resp = client.post(
            f"/api/v1/novels/{self.novel_id}/chapters/{other_chapter.id}/revisions/{revision.id}/restore"
        )

        self.assertEqual(detail_resp.status_code, 404)
        self.assertEqual(restore_resp.status_code, 404)
        self.assertEqual(self._fresh_chapter(other_chapter.id).content, "第二章正文")

    def test_missing_chapter_returns_404(self):
        resp = client.get(f"/api/v1/novels/{self.novel_id}/chapters/not-exist/revisions")

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["detail"], "章节不存在")


# ================= 4. 恢复语义 =================
class RevisionRestoreTest(RevisionTestBase):
    def test_restore_saves_current_state_then_rolls_back_fields(self):
        chapter = self._chapter()
        # v1 留档：开头。原文段落内容。结尾。
        revisions_uc.update_chapter_with_revision(
            self.db, self.novel_id, chapter.id, {"content": "第二版正文"}, revisions_uc.SOURCE_MANUAL_SAVE
        )
        first = self._revisions(chapter.id)[0]

        resp = client.post(
            f"/api/v1/novels/{self.novel_id}/chapters/{chapter.id}/revisions/{first.id}/restore"
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["content"], "开头。原文段落内容。结尾。")
        # 恢复前状态已另存为来源 restore 的快照，作者可再退回去
        revisions = self._revisions(chapter.id)
        self.assertEqual([r.revision_no for r in revisions], [1, 2])
        self.assertEqual(revisions[1].source, "restore")
        self.assertEqual(revisions[1].content, "第二版正文")

    def test_restore_clears_derived_data_and_memory(self):
        chapter = self._chapter()
        revisions_uc.update_chapter_with_revision(
            self.db, self.novel_id, chapter.id, {"content": "第二版正文"}, revisions_uc.SOURCE_MANUAL_SAVE
        )
        first = self._revisions(chapter.id)[0]
        # 构造「当前已定稿 + 有派生数据 + 有章节记忆」的状态
        repos.ChapterRepo(self.db).update(
            self.novel_id,
            chapter.id,
            status="finalized",
            summary="第二版摘要",
            tension_score=8.0,
            drift_score=0.7,
        )
        self._add_memory(chapter.id)
        self.assertGreater(self._memory_count(chapter.id), 0)

        resp = client.post(
            f"/api/v1/novels/{self.novel_id}/chapters/{chapter.id}/revisions/{first.id}/restore"
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["content"], "开头。原文段落内容。结尾。")
        self.assertEqual(body["title"], "第一章")
        self.assertEqual(body["directive"], "")
        self.assertEqual(body["status"], "draft")
        self.assertEqual(body["summary"], "")
        self.assertIsNone(body["tension_score"])
        self.assertIsNone(body["drift_score"])
        # 旧章节记忆必须删除，否则会召回已经被恢复掉的正文
        self.assertEqual(self._memory_count(chapter.id), 0)

    def test_restore_keeps_story_bible_untouched(self):
        chapter = self._chapter()
        character = repos.CharacterRepo(self.db).create(self.novel_id, name="林越")
        foreshadowing = repos.ForeshadowingRepo(self.db).create(
            self.novel_id, content="玉佩来历", planted_chapter_id=chapter.id
        )
        outline = repos.OutlineRepo(self.db).create(self.novel_id, title="第一卷", level="volume")
        revisions_uc.update_chapter_with_revision(
            self.db,
            self.novel_id,
            chapter.id,
            {"content": "第二版正文", "outline_node_id": outline.id},
            revisions_uc.SOURCE_MANUAL_SAVE,
        )
        first = self._revisions(chapter.id)[0]

        resp = client.post(
            f"/api/v1/novels/{self.novel_id}/chapters/{chapter.id}/revisions/{first.id}/restore"
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["outline_node_id"], outline.id)  # 大纲绑定不回滚
        self.assertEqual(resp.json()["seq"], chapter.seq)
        self.db.expire_all()
        self.assertEqual(repos.CharacterRepo(self.db).get(self.novel_id, character.id).name, "林越")
        kept = repos.ForeshadowingRepo(self.db).get(self.novel_id, foreshadowing.id)
        self.assertEqual(kept.planted_chapter_id, chapter.id)

    def test_restore_same_content_creates_no_duplicate_but_invalidates_derived_data(self):
        chapter = self._chapter()
        revisions_uc.update_chapter_with_revision(
            self.db, self.novel_id, chapter.id, {"content": "第二版正文"}, revisions_uc.SOURCE_MANUAL_SAVE
        )
        first = self._revisions(chapter.id)[0]
        # 把正文改回 v1 的内容，同时构造需要因恢复而失效的派生数据
        repos.ChapterRepo(self.db).update(
            self.novel_id,
            chapter.id,
            content="开头。原文段落内容。结尾。",
            status="finalized",
            tension_score=7.5,
            drift_score=0.4,
            summary="仍然是这一版的摘要",
        )
        self._add_memory(chapter.id)
        before = self._revision_count(chapter.id)

        resp = client.post(
            f"/api/v1/novels/{self.novel_id}/chapters/{chapter.id}/revisions/{first.id}/restore"
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._revision_count(chapter.id), before)  # 不制造重复快照
        fresh = self._fresh_chapter(chapter.id)
        self.assertEqual(fresh.status, "draft")
        self.assertEqual(fresh.summary, "")
        self.assertIsNone(fresh.tension_score)
        self.assertIsNone(fresh.drift_score)
        self.assertEqual(self._memory_count(chapter.id), 0)

    def test_restore_then_recover_current_via_new_snapshot(self):
        chapter = self._chapter()
        revisions_uc.update_chapter_with_revision(
            self.db, self.novel_id, chapter.id, {"content": "第二版正文"}, revisions_uc.SOURCE_MANUAL_SAVE
        )
        first = self._revisions(chapter.id)[0]
        client.post(f"/api/v1/novels/{self.novel_id}/chapters/{chapter.id}/revisions/{first.id}/restore")
        restore_snapshot = [r for r in self._revisions(chapter.id) if r.source == "restore"][0]

        resp = client.post(
            f"/api/v1/novels/{self.novel_id}/chapters/{chapter.id}/revisions/{restore_snapshot.id}/restore"
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["content"], "第二版正文")


# ================= 5. 删除清理 =================
class RevisionDeleteTest(RevisionTestBase):
    def test_delete_chapter_removes_its_revisions(self):
        chapter = self._chapter()
        revisions_uc.update_chapter_with_revision(
            self.db, self.novel_id, chapter.id, {"content": "第二版正文"}, revisions_uc.SOURCE_MANUAL_SAVE
        )
        self.assertEqual(self._revision_count(chapter.id), 1)

        resp = client.delete(f"/api/v1/novels/{self.novel_id}/chapters/{chapter.id}")

        self.assertEqual(resp.status_code, 204, resp.text)
        self.assertEqual(self._revision_count(chapter.id), 0)
        self.assertEqual(
            self.db.query(models.ChapterRevision)
            .filter(models.ChapterRevision.novel_id == self.novel_id)
            .count(),
            0,
        )

    def test_delete_novel_removes_all_revisions(self):
        chapter = self._chapter()
        revisions_uc.update_chapter_with_revision(
            self.db, self.novel_id, chapter.id, {"content": "第二版正文"}, revisions_uc.SOURCE_MANUAL_SAVE
        )
        self.assertGreater(self._revision_count(chapter.id), 0)

        resp = client.delete(f"/api/v1/novels/{self.novel_id}")

        self.assertEqual(resp.status_code, 204, resp.text)
        self.db.expire_all()
        self.assertEqual(
            self.db.query(models.ChapterRevision)
            .filter(models.ChapterRevision.novel_id == self.novel_id)
            .count(),
            0,
        )


if __name__ == "__main__":
    unittest.main()
