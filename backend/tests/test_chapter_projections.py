"""统计精确投影：真实SQL列、当前数据、调用方事务与旧全文合同。"""
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.application import delivery as delivery_uc, quality as quality_uc
from app.application.revisions import count_words
from app.infrastructure import models, repository as repos
from app.infrastructure.db import get_db
from app.interfaces.api.v1 import chapters, delivery, export, quality


class ChapterProjectionTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="lingmo-stat-projection-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "statistics.sqlite"
        self.engine = create_engine(f"sqlite:///{self.path.as_posix()}", connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        models.Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        self.nid = repos.NovelRepo(self.db).create(title="投影作品", style_anchor="古风").id
        self.other = repos.NovelRepo(self.db).create(title="其他作品").id
        self.base = f"/api/v1/novels/{self.nid}"
        app = FastAPI()
        for router in (quality.router, delivery.router, chapters.router, export.router):
            app.include_router(router, prefix="/api/v1")

        def isolated_db():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = isolated_db
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.cursors, self.chapter_loads = [], []

        def after_sql(_conn, cursor, statement, *_):
            if cursor.description:
                self.cursors.append((statement, [column[0] for column in cursor.description]))

        def chapter_load(row, _context):
            self.chapter_loads.append(row.id)

        event.listen(self.engine, "after_cursor_execute", after_sql)
        event.listen(models.Chapter, "load", chapter_load)
        self.addCleanup(event.remove, self.engine, "after_cursor_execute", after_sql)
        self.addCleanup(event.remove, models.Chapter, "load", chapter_load)
        for target in ("httpx.HTTPTransport.handle_request", "httpx.AsyncHTTPTransport.handle_async_request",
                       "app.infrastructure.ai.embedding.EmbeddingService.embed",
                       "app.infrastructure.ai.llm_provider.LLMProvider.complete",
                       "app.infrastructure.ai.llm_provider.LLMProvider.stream_complete"):
            guard = patch(target, side_effect=AssertionError("统计投影禁止调用网络或模型"))
            guard.start()
            self.addCleanup(guard.stop)

    def chapter(self, **fields):
        values = {"title": "章节", "content": "甲 乙😀", "summary": "完整摘要", "directive": "完整指令"}
        values.update(fields)
        return repos.ChapterRepo(self.db).create(values.pop("novel_id", self.nid), **values).id

    def clue(self, **fields):
        return repos.ForeshadowingRepo(self.db).create(self.nid, **fields).id

    def get(self, suffix):
        response = self.client.get(self.base + suffix)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_hundred_large_chapters_use_exact_five_three_eight_cursor_columns_and_zero_chapter_loads(self):
        body = ("山路曲折，风过松林，同行者停下脚步，回望来处。" * 300)[:6000]
        self.db.add_all([models.Chapter(novel_id=self.nid, seq=i + 1, title=f"第{i + 1}章", content=body,
                        summary="摘要" * 2000, directive="指令" * 2000) for i in range(100)])
        self.db.commit()
        cases = (
            ("/quality/curve", ["chapter_seq", "chapter_title", "chapter_status", "chapter_tension_score", "chapter_drift_score"], 3),
            ("/quality/foreshadowings", ["chapter_id", "chapter_seq", "is_written"], 5),
            ("/book/summary", ["chapter_id", "chapter_seq", "chapter_title", "chapter_status", "chapter_outline_node_id",
                               "chapter_tension_score", "chapter_drift_score", "word_count"], 5),
        )
        for suffix, columns, read_count in cases:
            with self.subTest(suffix=suffix):
                self.cursors.clear(); self.chapter_loads.clear()
                result = self.get(suffix)
                query, actual = next(row for row in self.cursors if "FROM chapter" in row[0])
                self.assertEqual(actual, columns)
                self.assertEqual(len(self.cursors), read_count)
                self.assertEqual(self.chapter_loads, [])
                self.assertNotIn("chapter.summary", query)
                self.assertNotIn("chapter.directive", query)
                self.assertNotIn("chapter.created_at", query)
                self.assertNotIn("chapter.updated_at", query)
                if suffix == "/book/summary":
                    self.assertEqual((result["chapter_count"], result["word_count"]), (100, 600000))
                elif suffix == "/quality/foreshadowings":
                    self.assertEqual((result["chapter_count"], result["latest_seq"]), (100, 100))
                else:
                    self.assertEqual([row["seq"] for row in result["chapters"]], list(range(1, 101)))

    def test_unicode_written_flag_preserves_strip_and_book_count_preserves_all_whitespace_emoji_and_nul(self):
        whitespace = "".join(chr(cp) for cp in range(0x110000) if chr(cp).isspace())
        values = ["", whitespace, "😀𠮷👨‍👩‍👧‍👦", "甲\x00乙", "\x00", "\u200b\ufeff", "甲" + whitespace + "乙😀"]
        ids = [self.chapter(seq=index + 1, content=value) for index, value in enumerate(values)]
        rows = {row.id: row for row in repos.ChapterRepo(self.db).list_tracking_chapters(self.nid)}
        counts = {row.id: row.word_count for row in repos.ChapterRepo(self.db).list_book_metadata(self.nid)}
        for cid, value in zip(ids, values):
            with self.subTest(value=repr(value)):
                self.assertEqual(bool(rows[cid].is_written), bool(value.strip()))
                self.assertEqual(counts[cid], count_words(value))
        self.assertEqual(repos._chapter_is_written(None), False)
        self.assertEqual(self.db.execute(text("SELECT lingmo_chapter_is_written(NULL)")).scalar_one(), 0)
        self.assertEqual(self.db.execute(text("SELECT lingmo_chapter_word_count(NULL)")).scalar_one(), 0)
        self.assertEqual(self.get("/quality/foreshadowings")["chapter_count"], 5)
        self.assertEqual(self.get("/book/summary")["word_count"], sum(map(count_words, values)))

    def test_tracking_keeps_negative_gaps_unknown_empty_foreign_refs_threshold_and_original_order(self):
        first = self.chapter(seq=1)
        second = self.chapter(seq=2)
        third = self.chapter(seq=3)
        empty = self.chapter(seq=20, content="\u3000\u0085")
        foreign = self.chapter(novel_id=self.other, seq=99)
        same = self.clue(content="同章闭合", status="consumed", planted_chapter_id=first, consumed_chapter_id=first)
        reverse = self.clue(content="逆序闭合", status="consumed", planted_chapter_id=third, consumed_chapter_id=first)
        pending = self.clue(content="长期悬置", status="suspended", planted_chapter_id=first)
        later = self.clue(content="后埋", status="planted", planted_chapter_id=second)
        missing = self.clue(content="缺引用", status="suspended", planted_chapter_id="不存在")
        blank = self.clue(content="空正文引用", status="planted", planted_chapter_id=empty)
        outside = self.clue(content="旧跨作品引用", status="consumed", planted_chapter_id=foreign, consumed_chapter_id=third)
        data = self.get("/quality/foreshadowings")
        by_id = {row["id"]: row for group in data["groups"].values() for row in group}
        self.assertEqual((data["latest_seq"], data["chapter_count"]), (3, 3))
        self.assertEqual(data["long_pending_threshold"], {"chapters": 10, "ratio": .3})
        self.assertEqual([row["id"] for row in data["groups"]["consumed"]], [same, reverse, outside])
        self.assertEqual([row["id"] for row in data["groups"]["suspended"]], [pending, missing])
        self.assertEqual([row["id"] for row in data["groups"]["planted"]], [later, blank])
        self.assertEqual(by_id[same]["suspended_chapters"], -1)
        self.assertEqual(by_id[reverse]["suspended_chapters"], -3)
        self.assertTrue(by_id[pending]["long_pending"])
        for cid in (missing, blank, outside):
            self.assertIsNone(by_id[cid]["planted_seq"])
            self.assertIsNone(by_id[cid]["suspended_chapters"])

    def test_book_keeps_volume_chain_order_status_and_foreign_or_broken_outline_unbound(self):
        volume = repos.OutlineRepo(self.db).create(self.nid, title="本卷", level="volume").id
        act = repos.OutlineRepo(self.db).create(self.nid, title="幕", level="act", parent_id=volume).id
        node = repos.OutlineRepo(self.db).create(self.nid, title="章", parent_id=act).id
        foreign = repos.OutlineRepo(self.db).create(self.other, title="外卷", level="volume").id
        later = self.chapter(seq=10, status="draft", outline_node_id=foreign, content="\t")
        first = self.chapter(seq=2, title="定稿章", status="finalized", outline_node_id=node, content="甲\x00😀", tension_score=7, drift_score=.2)
        broken = self.chapter(seq=11, outline_node_id="已删除节点", content="乙")
        data = self.get("/book/summary")
        self.assertEqual(data["volumes"], [{"id": volume, "title": "本卷"}])
        self.assertEqual([row["id"] for row in data["chapters"]], [first, later, broken])
        self.assertEqual((data["chapter_count"], data["draft_count"], data["finalized_count"], data["word_count"]), (3, 2, 1, 4))
        self.assertEqual((data["chapters"][0]["volume_id"], data["chapters"][0]["volume_title"]), (volume, "本卷"))
        for row in data["chapters"][1:]:
            self.assertEqual((row["volume_id"], row["volume_title"]), ("", ""))

    def test_projection_bypasses_strong_old_chapter_identity_for_scores_title_and_written_content(self):
        cid = self.chapter(content="原稿", tension_score=1, drift_score=.1)
        cached = repos.ChapterRepo(self.db).get(self.nid, cid)
        with self.Session() as other:
            other.execute(text("UPDATE chapter SET content='  ',title='已保存新标题',tension_score=9,drift_score=.8,status='finalized' WHERE id=:cid"), {"cid": cid})
            other.commit()
        self.chapter_loads.clear()
        curve = quality_uc.build_curve(self.db, self.nid)["chapters"][0]
        track = quality_uc.aggregate_foreshadowings(self.db, self.nid)
        book = delivery_uc.book_summary(self.db, self.nid)["chapters"][0]
        self.assertEqual((curve["title"], curve["tension_score"], curve["drift_score"], curve["status"]), ("已保存新标题", 9, .8, "finalized"))
        self.assertEqual(track["chapter_count"], 0)
        self.assertEqual((book["title"], book["word_count"], book["tension_score"]), ("已保存新标题", 0, 9))
        self.assertEqual((cached.content, cached.tension_score), ("原稿", 1))
        self.assertEqual(self.chapter_loads, [])

    def test_implicit_autoflush_and_explicit_no_autoflush_keep_original_query_contract_without_commit_or_rollback(self):
        cid = self.chapter(tension_score=1)
        operations = (quality_uc.build_curve, quality_uc.aggregate_foreshadowings, delivery_uc.book_summary)
        for no_flush in (False, True):
            for operation in operations:
                with self.subTest(no_flush=no_flush, operation=operation.__name__), self.Session(autoflush=True) as db:
                    row = repos.ChapterRepo(db).get(self.nid, cid)
                    row.content = "  "
                    row.tension_score = 9
                    with patch.object(db, "commit", wraps=db.commit) as commit, patch.object(db, "rollback", wraps=db.rollback) as rollback:
                        with db.no_autoflush if no_flush else nullcontext():
                            result = operation(db, self.nid)
                        commit.assert_not_called(); rollback.assert_not_called()
                    if operation is quality_uc.aggregate_foreshadowings:
                        self.assertEqual(result["chapter_count"], 1 if no_flush else 0)
                    else:
                        self.assertEqual(result["chapters"][0]["tension_score"], 1 if no_flush else 9)
                    self.assertEqual(row in db.dirty, no_flush)
                    with self.Session() as outside:
                        self.assertEqual(repos.ChapterRepo(outside).get(self.nid, cid).tension_score, 1)
                    db.rollback()

    def test_core_and_flushed_orm_writes_are_visible_and_still_caller_owned(self):
        cid = self.chapter(tension_score=1)
        methods = ("list_quality_curve", "list_tracking_chapters", "list_book_metadata")
        for mode in ("core", "flushed_orm"):
            for method in methods:
                with self.subTest(mode=mode, method=method), self.Session() as db:
                    if mode == "core":
                        db.execute(text("UPDATE chapter SET content=' ',tension_score=8 WHERE id=:cid"), {"cid": cid})
                    else:
                        row = repos.ChapterRepo(db).get(self.nid, cid)
                        row.content = " "
                        row.tension_score = 8
                        db.flush()
                    with patch.object(db, "commit", wraps=db.commit) as commit, patch.object(db, "rollback", wraps=db.rollback) as rollback:
                        projected = getattr(repos.ChapterRepo(db), method)(self.nid)[0]
                        commit.assert_not_called(); rollback.assert_not_called()
                    self.assertTrue(db.connection().connection.driver_connection.in_transaction)
                    if method == "list_tracking_chapters":
                        self.assertFalse(projected.is_written)
                    else:
                        self.assertEqual(projected.tension_score, 8)
                    with self.Session() as outside:
                        self.assertEqual(repos.ChapterRepo(outside).get(self.nid, cid).tension_score, 1)
                    db.rollback()

    def test_deterministic_functions_register_on_existing_multiple_and_fresh_connections_without_cross_scope(self):
        self.chapter(content="甲😀\x00乙")
        first_driver = self.db.connection().connection.driver_connection
        with self.Session() as second:
            self.assertIsNot(second.connection().connection.driver_connection, first_driver)
            self.assertEqual(repos.ChapterRepo(second).list_tracking_chapters(self.nid)[0].is_written, 1)
            self.assertEqual(repos.ChapterRepo(second).list_book_metadata(self.nid)[0].word_count, 4)
        fresh_engine = create_engine(f"sqlite:///{self.path.as_posix()}", poolclass=NullPool)
        self.addCleanup(fresh_engine.dispose)
        for _ in range(2):
            with sessionmaker(bind=fresh_engine, autoflush=False)() as db:
                repo = repos.ChapterRepo(db)
                self.assertEqual(repo.list_tracking_chapters(self.nid)[0].is_written, 1)
                self.assertEqual(repo.list_book_metadata(self.nid)[0].word_count, 4)
                self.assertEqual(repo.list_tracking_chapters(self.other), [])
                flags = {row[0]: row[-1] for row in db.execute(text("PRAGMA function_list"))}
                for name in ("lingmo_chapter_is_written", "lingmo_chapter_word_count"):
                    self.assertTrue(flags[name] & 0x800)

    def test_empty_unknown_and_foreign_novel_keep_404_and_response_contracts(self):
        for suffix in ("/quality/curve", "/quality/foreshadowings", "/book/summary"):
            self.assertEqual(self.client.get("/api/v1/novels/missing" + suffix).status_code, 404)
        self.assertEqual(self.get("/quality/curve")["chapters"], [])
        self.assertEqual(self.get("/quality/foreshadowings")["groups"], {"planted": [], "suspended": [], "consumed": []})
        self.assertEqual(self.get("/book/summary")["word_count"], 0)
        self.chapter(novel_id=self.other, seq=1, content="外部作品")
        self.assertEqual(self.get("/book/summary")["chapters"], [])

    def test_full_detail_metadata_export_and_preflight_keep_bodies_and_default_draft_exclusion(self):
        finalized = self.chapter(seq=1, content="成品甲😀", status="finalized")
        self.chapter(seq=2, content="未确认草稿乙", status="draft")
        for suffix in ("/quality/curve", "/quality/foreshadowings", "/book/summary"):
            self.get(suffix)
        detail = self.get(f"/chapters/{finalized}")
        self.assertEqual(self.get("/chapters")[0], detail)
        self.assertEqual((detail["content"], detail["summary"], detail["directive"]), ("成品甲😀", "完整摘要", "完整指令"))
        metadata = self.get("/chapters/metadata")
        self.assertEqual(len(metadata[0]), 11)
        response = self.client.get(self.base + "/export/txt")
        self.assertIn("成品甲😀", response.text)
        self.assertNotIn("未确认草稿乙", response.text)
        response = self.client.post(self.base + "/export/file", json={"format": "txt", "include_drafts": True})
        self.assertIn("未确认草稿乙", response.text)
        response = self.client.post(self.base + "/export/preflight", json={"scope": "all"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["chapter_count"], 1)
        self.assertEqual(response.json()["word_count"], count_words("成品甲😀"))

    def test_statistics_do_not_mutate_schema_or_saved_chapters(self):
        self.chapter()
        with self.engine.connect() as conn:
            before_schema = conn.execute(text("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")).all()
            before_rows = conn.execute(text("SELECT * FROM chapter ORDER BY id")).all()
        for suffix in ("/quality/curve", "/quality/foreshadowings", "/book/summary"):
            self.get(suffix)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")).all(), before_schema)
            self.assertEqual(conn.execute(text("SELECT * FROM chapter ORDER BY id")).all(), before_rows)


if __name__ == "__main__":
    unittest.main()
