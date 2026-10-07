"""章节目录投影：精确计数、当前来源、连接注册与全文/交付兼容。"""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.application.revisions import count_words as revision_words
from app.application.word_count import count_words as content_words
from app.infrastructure import models, repository as repos
from app.infrastructure.db import get_db
from app.interfaces.api.v1 import chapters, delivery, export


FIELDS = {"id", "novel_id", "seq", "title", "status", "outline_node_id", "created_at", "updated_at",
          "tension_score", "drift_score", "word_count"}


class ChapterMetadataTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="lingmo-chapter-metadata-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "chapters.sqlite"
        self.engine = create_engine(f"sqlite:///{self.path.as_posix()}", connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        models.Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        self.nid = repos.NovelRepo(self.db).create(title="目录作品").id
        self.other = repos.NovelRepo(self.db).create(title="另一作品").id
        self.base = f"/api/v1/novels/{self.nid}"
        app = FastAPI()
        for router in (chapters.router, delivery.router, export.router):
            app.include_router(router, prefix="/api/v1")

        def isolated_db():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = isolated_db
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.statements, self.chapter_loads = [], []
        sql_listener = lambda _conn, _cursor, statement, *_args: self.statements.append(statement)
        load_listener = lambda row, _context: self.chapter_loads.append(row.id)
        event.listen(self.engine, "before_cursor_execute", sql_listener)
        event.listen(models.Chapter, "load", load_listener)
        self.addCleanup(event.remove, self.engine, "before_cursor_execute", sql_listener)
        self.addCleanup(event.remove, models.Chapter, "load", load_listener)
        for target in ("httpx.HTTPTransport.handle_request", "httpx.AsyncHTTPTransport.handle_async_request",
                       "app.infrastructure.ai.embedding.EmbeddingService.embed",
                       "app.infrastructure.ai.llm_provider.LLMProvider.complete",
                       "app.infrastructure.ai.llm_provider.LLMProvider.stream_complete"):
            guard = patch(target, side_effect=AssertionError("目录不能调用网络或模型"))
            guard.start()
            self.addCleanup(guard.stop)

    def create(self, *, novel_id=None, **extra):
        fields = {"title": "已保存标题", "content": "甲 乙😀", "summary": "摘要只出现在详情", "directive": "指令只出现在详情"}
        fields.update(extra)
        return repos.ChapterRepo(self.db).create(novel_id or self.nid, **fields).id

    def metadata(self):
        response = self.client.get(self.base + "/chapters/metadata")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_static_metadata_route_exact_fields_scope_and_numeric_sequence_order(self):
        c10 = self.create(seq=10, status="finalized", tension_score=8.2, drift_score=.1)
        c2 = self.create(seq=2)
        foreign = self.create(novel_id=self.other, seq=1, content="另一作品正文")
        rows = self.metadata()
        self.assertEqual([row["id"] for row in rows], [c2, c10])
        self.assertNotIn(foreign, [row["id"] for row in rows])
        self.assertTrue(all(set(row) == FIELDS and row["novel_id"] == self.nid for row in rows))
        self.assertEqual(rows[0]["word_count"], 3)
        self.assertEqual((rows[1]["status"], rows[1]["tension_score"], rows[1]["drift_score"]), ("finalized", 8.2, .1))
        self.assertEqual((rows[0]["tension_score"], rows[0]["drift_score"]), (None, None))

    def test_empty_existing_work_returns_array_and_unknown_work_is_404(self):
        self.assertEqual(self.metadata(), [])
        response = self.client.get("/api/v1/novels/not-present/chapters/metadata")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "作品不存在")

    def test_unicode_whitespace_emoji_nul_and_zero_width_counts_match_existing_contracts(self):
        whitespace = "".join(chr(cp) for cp in range(0x110000) if chr(cp).isspace())
        values = ["", whitespace, " \t林\n舟，😀\r\n\u3000\u00a0\u0085\u001c", "甲\x00乙",
                  "😀𠮷👨‍👩‍👧‍👦", "甲\u200b\ufeff乙", "甲" + whitespace + "乙\x00😀"]
        ids = [self.create(seq=index + 1, content=value) for index, value in enumerate(values)]
        rows = {row["id"]: row for row in self.metadata()}
        for cid, value in zip(ids, values):
            with self.subTest(value=repr(value)):
                self.assertEqual(rows[cid]["word_count"], revision_words(value))
                self.assertEqual(rows[cid]["word_count"], content_words(value))
        self.assertEqual(rows[ids[3]]["word_count"], 3)
        self.assertEqual(rows[ids[4]]["word_count"], 9)

    def test_true_projection_has_two_reads_zero_chapter_orm_loads_and_no_large_text_fields(self):
        self.create(content="正文标记" * 2000, summary="摘要标记" * 2000, directive="指令标记" * 2000)
        self.statements.clear(); self.chapter_loads.clear()
        rows = self.metadata()
        self.assertEqual(self.chapter_loads, [])
        self.assertEqual(len(self.statements), 2)
        chapter_select = next(statement for statement in self.statements if "FROM chapter" in statement)
        self.assertIn("lingmo_chapter_word_count(chapter.content)", chapter_select)
        self.assertNotIn("chapter.summary", chapter_select)
        self.assertNotIn("chapter.directive", chapter_select)
        self.assertNotIn("chapter.content,", chapter_select)
        self.assertEqual(set(rows[0]), FIELDS)

    def test_metadata_bypasses_cached_chapter_after_another_connection_changes_saved_fields(self):
        cid = self.create(content="原稿", tension_score=1.0)
        cached = repos.ChapterRepo(self.db).get(self.nid, cid)
        with self.Session() as other:
            repos.ChapterRepo(other).update(self.nid, cid, content="新 稿😀\x00", title="另一请求新标题", tension_score=5.0)
        self.assertEqual(cached.content, "原稿")
        self.chapter_loads.clear()
        row = repos.ChapterRepo(self.db).list_metadata(self.nid)[0]
        self.assertEqual((row.title, row.word_count, row.tension_score), ("另一请求新标题", 4, 5.0))
        self.assertEqual(cached.content, "原稿")
        self.assertEqual(self.chapter_loads, [])

    def test_actual_save_and_restore_immediately_change_count_and_derived_metadata(self):
        cid = self.create(content="旧😀", status="finalized", tension_score=8.0, drift_score=.2)
        before = self.metadata()[0]
        response = self.client.put(self.base + f"/chapters/{cid}", json={"content": "新\x00稿\u3000😀，"})
        self.assertEqual(response.status_code, 200)
        changed = self.metadata()[0]
        self.assertEqual(changed["word_count"], 5)
        self.assertEqual((changed["status"], changed["tension_score"], changed["drift_score"]), ("draft", None, None))
        self.assertNotEqual(changed["updated_at"], before["updated_at"])
        revision = self.client.get(self.base + f"/chapters/{cid}/revisions").json()[0]
        restored = self.client.post(self.base + f"/chapters/{cid}/revisions/{revision['id']}/restore")
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(self.metadata()[0]["word_count"], 2)

    def test_complete_list_and_single_chapter_still_return_exact_full_saved_content(self):
        content = "甲\x00乙😀\n正文"
        cid = self.create(content=content)
        self.metadata()
        listed = self.client.get(self.base + "/chapters").json()
        detail = self.client.get(self.base + f"/chapters/{cid}").json()
        self.assertEqual(listed[0], detail)
        self.assertEqual(detail["content"], content)
        self.assertEqual(detail["summary"], "摘要只出现在详情")
        self.assertEqual(detail["directive"], "指令只出现在详情")
        self.assertNotIn("word_count", detail)
        self.assertEqual(self.client.get(f"/api/v1/novels/{self.other}/chapters/{cid}").status_code, 404)

    def test_metadata_does_not_change_default_draft_exclusion_or_full_text_export(self):
        self.create(seq=1, content="成品正文甲😀", status="finalized")
        self.create(seq=2, content="尚未确认的草稿正文乙", status="draft")
        self.metadata()
        exported = self.client.get(self.base + "/export/txt")
        self.assertEqual(exported.status_code, 200)
        self.assertIn("成品正文甲😀", exported.text)
        self.assertNotIn("尚未确认的草稿正文乙", exported.text)
        explicit = self.client.post(self.base + "/export/file", json={"format": "txt", "include_drafts": True})
        self.assertEqual(explicit.status_code, 200)
        self.assertIn("成品正文甲😀", explicit.text)
        self.assertIn("尚未确认的草稿正文乙", explicit.text)

    def test_registration_works_on_preexisting_multiple_and_new_pool_connections(self):
        self.create(content="甲😀\x00乙")
        first_driver = self.db.connection().connection.driver_connection
        with self.Session() as second:
            second_driver = second.connection().connection.driver_connection
            self.assertIsNot(second_driver, first_driver)
            self.assertEqual(repos.ChapterRepo(second).list_metadata(self.nid)[0].word_count, 4)
        self.assertEqual(repos.ChapterRepo(self.db).list_metadata(self.nid)[0].word_count, 4)
        self.assertEqual(repos.ChapterRepo(self.db).list_metadata(self.nid)[0].word_count, 4)
        fresh_engine = create_engine(f"sqlite:///{self.path.as_posix()}", poolclass=NullPool)
        self.addCleanup(fresh_engine.dispose)
        for _ in range(2):
            with sessionmaker(bind=fresh_engine, autoflush=False)() as fresh:
                self.assertEqual(repos.ChapterRepo(fresh).list_metadata(self.nid)[0].word_count, 4)
                flags = next(row[-1] for row in fresh.execute(text("PRAGMA function_list"))
                             if row[0] == "lingmo_chapter_word_count")
                self.assertTrue(flags & 0x800)  # SQLite SQLITE_DETERMINISTIC

    def test_registration_and_projection_preserve_caller_core_or_flushed_orm_transaction(self):
        cid = self.create(content="原稿")
        for mode in ("core", "flushed_orm"):
            with self.subTest(mode=mode):
                if mode == "core":
                    self.db.execute(text("UPDATE chapter SET content='调用方 未提交😀' WHERE id=:cid"), {"cid": cid})
                else:
                    row = repos.ChapterRepo(self.db).get(self.nid, cid)
                    row.content = "调用方 未提交😀"
                    self.db.flush()
                metadata = repos.ChapterRepo(self.db).list_metadata(self.nid)
                self.assertEqual(metadata[0].word_count, revision_words("调用方 未提交😀"))
                self.assertTrue(self.db.connection().connection.driver_connection.in_transaction)
                with self.Session() as outside:
                    self.assertEqual(repos.ChapterRepo(outside).get(self.nid, cid).content, "原稿")
                self.db.rollback()
                self.assertEqual(repos.ChapterRepo(self.db).get(self.nid, cid).content, "原稿")

    def test_metadata_has_no_schema_migration_or_database_mutation(self):
        cid = self.create()
        with self.engine.connect() as conn:
            schema_before = conn.execute(text("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")).all()
            rows_before = conn.execute(text("SELECT * FROM chapter WHERE id=:cid"), {"cid": cid}).all()
        self.metadata()
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")).all(), schema_before)
            self.assertEqual(conn.execute(text("SELECT * FROM chapter WHERE id=:cid"), {"cid": cid}).all(), rows_before)
            self.assertNotIn("word_count", [row[1] for row in conn.execute(text("PRAGMA table_info(chapter)"))])

    def test_hundred_large_chapters_return_small_metadata_and_current_correct_total(self):
        body = ("山路曲折，风过松林，同行者停下脚步，回望来处。" * 300)[:6000]
        self.db.add_all([models.Chapter(novel_id=self.nid, seq=index + 1, title=f"第{index + 1}章", content=body,
                                       summary="摘要" * 200, directive="章前指令") for index in range(100)])
        self.db.commit()
        full = self.client.get(self.base + "/chapters")
        self.chapter_loads.clear()
        metadata = self.client.get(self.base + "/chapters/metadata")
        self.assertEqual(metadata.status_code, 200)
        self.assertEqual(len(metadata.json()), 100)
        self.assertEqual(sum(row["word_count"] for row in metadata.json()), 600000)
        self.assertLess(len(metadata.content), len(full.content) / 25)
        self.assertEqual(self.chapter_loads, [])


if __name__ == "__main__":
    unittest.main()
