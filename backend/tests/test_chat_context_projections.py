"""聊天上下文有界列查询：真实SQLite排序、缓存与调用方事务合同。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from app.application import chat
from app.infrastructure import models, repository as repos


class ChatContextProjectionTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.engine = create_engine(f"sqlite:///{Path(temporary.name).as_posix()}/context.db")
        self.addCleanup(self.engine.dispose)
        models.Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=True)
        with self.sessions() as db:
            novel = models.Novel(title="当前作品")
            other = models.Novel(title="另一本作品")
            db.add_all([novel, other])
            db.flush()
            conv = models.Conversation(title="当前会话", novel_id=novel.id)
            foreign = models.Conversation(title="其它会话", novel_id=other.id)
            db.add_all([conv, foreign])
            db.flush()
            self.nid, self.other_nid, self.cid, self.other_cid = novel.id, other.id, conv.id, foreign.id
            db.commit()

    def seed(self, *, messages=0, chapters=0, tied=False):
        with self.sessions() as db:
            for index in range(messages):
                db.add(models.Message(conversation_id=self.cid, role="user" if index % 2 else "assistant",
                    content=f"消息{index}😀\x00", meta={"original": index},
                    created_at="2026-01-01T01:00:00" if tied else f"2026-01-01T01:{index:02}:00"))
            for index in range(chapters):
                db.add(models.Chapter(novel_id=self.nid, seq=7 if tied else index + 1,
                    title=f"章{index}", content="正文" * 3000, directive="仅作者指令",
                    summary=f"摘要{index}😀\x00", status="finalized"))
            db.commit()

    @staticmethod
    def values(rows, names):
        return [tuple(getattr(row, name) for name in names) for row in rows]

    def test_message_ties_keep_old_tail_twenty_in_original_row_order(self):
        self.seed(messages=50, tied=True)
        with self.sessions() as db:
            expected = self.values(repos.ConversationRepo(db).list_messages(self.cid)[-20:], ("role", "content"))
        with self.sessions() as db:
            actual = self.values(repos.ConversationRepo(db).recent_messages(self.cid), ("role", "content"))
        self.assertEqual(actual, expected)
        self.assertEqual(actual[0][1], "消息30😀\x00")
        self.assertEqual(actual[-1][1], "消息49😀\x00")

    def test_chapter_seq_ties_keep_old_last_three_in_original_row_order(self):
        self.seed(chapters=8, tied=True)
        with self.sessions() as db:
            expected = self.values(repos.ChapterRepo(db).finalized(self.nid)[-3:], ("seq", "summary"))
        with self.sessions() as db:
            actual = self.values(repos.ChapterRepo(db).recent_finalized_summaries(self.nid), ("seq", "summary"))
        self.assertEqual(actual, expected)
        self.assertEqual([row[1] for row in actual], [f"摘要{i}😀\x00" for i in (5, 6, 7)])

    def test_latest_three_empty_summaries_are_not_filled_from_older_chapters(self):
        self.seed(chapters=8)
        with self.sessions() as db:
            db.execute(models.Chapter.__table__.update().where(models.Chapter.seq >= 6).values(summary=""))
            db.commit()
        with self.sessions() as db:
            rows = repos.ChapterRepo(db).recent_finalized_summaries(self.nid)
            self.assertEqual([row.seq for row in rows], [6, 7, 8])
            self.assertEqual(chat.build_chat_context(db, self.nid), "")

    def test_newest_cutoff_status_and_scope_are_exact(self):
        self.seed(messages=22, chapters=4)
        with self.sessions() as db:
            db.add(models.Message(conversation_id=self.cid, role="user", content="最早",
                created_at="2025-01-01"))
            db.add(models.Message(conversation_id=self.other_cid, role="user", content="外国消息", created_at="2099"))
            db.add(models.Chapter(novel_id=self.nid, seq=999, summary="未定稿", status="draft"))
            db.add(models.Chapter(novel_id=self.other_nid, seq=999, summary="外国摘要", status="finalized"))
            db.commit()
        with self.sessions() as db:
            messages = repos.ConversationRepo(db).recent_messages(self.cid)
            chapters = repos.ChapterRepo(db).recent_finalized_summaries(self.nid)
            self.assertEqual([row.content for row in messages], [f"消息{i}😀\x00" for i in range(2, 22)])
            self.assertEqual([row.seq for row in chapters], [2, 3, 4])
            self.assertEqual(repos.ConversationRepo(db).recent_messages(self.other_cid)[0].content, "外国消息")
            with self.assertRaises(HTTPException) as error:
                repos.ConversationRepo(db).recent_messages("missing")
            self.assertEqual(error.exception.status_code, 404)
            self.assertEqual(repos.ChapterRepo(db).recent_finalized_summaries("missing"), [])

    def test_projections_select_only_two_columns_and_load_no_history_entities(self):
        self.seed(messages=100, chapters=100)
        columns, loaded = [], []

        def cursor(_conn, cursor, statement, *_args):
            if statement.lstrip().upper().startswith("SELECT") and cursor.description:
                names = [column[0] for column in cursor.description]
                if "message_role" in names or "chapter_seq" in names:
                    columns.append(names)

        event.listen(self.engine, "after_cursor_execute", cursor)
        self.addCleanup(event.remove, self.engine, "after_cursor_execute", cursor)
        with self.sessions() as db:
            event.listen(db, "loaded_as_persistent", lambda _db, obj: loaded.append(type(obj)))
            self.assertEqual(len(repos.ConversationRepo(db).recent_messages(self.cid)), 20)
            self.assertEqual(len(repos.ChapterRepo(db).recent_finalized_summaries(self.nid)), 3)
        self.assertEqual(columns, [["message_role", "message_content"], ["chapter_seq", "chapter_summary"]])
        self.assertNotIn(models.Message, loaded)
        self.assertNotIn(models.Chapter, loaded)

    def test_default_autoflush_preserves_pending_insert_visibility_without_commit(self):
        with self.sessions() as db:
            message = models.Message(conversation_id=self.cid, role="user", content="尚未提交")
            chapter = models.Chapter(novel_id=self.nid, seq=1, status="finalized", summary="未提交摘要")
            db.add_all([message, chapter])
            with patch.object(db, "commit") as commit, patch.object(db, "rollback") as rollback:
                self.assertEqual(repos.ConversationRepo(db).recent_messages(self.cid)[0].content, "尚未提交")
                self.assertEqual(repos.ChapterRepo(db).recent_finalized_summaries(self.nid)[0].summary, "未提交摘要")
                commit.assert_not_called()
                rollback.assert_not_called()
            self.assertNotIn(message, db.new)
            db.rollback()
        with self.sessions() as db:
            self.assertEqual(repos.ConversationRepo(db).recent_messages(self.cid), [])
            self.assertEqual(repos.ChapterRepo(db).recent_finalized_summaries(self.nid), [])

    def test_explicit_no_autoflush_and_autoflush_false_preserve_pending(self):
        for enabled in (True, False):
            with self.subTest(autoflush=enabled), self.sessions(autoflush=enabled) as db:
                message = models.Message(conversation_id=self.cid, role="user", content="pending")
                chapter = models.Chapter(novel_id=self.nid, seq=1, status="finalized", summary="pending")
                db.add_all([message, chapter])
                with db.no_autoflush, patch.object(db, "flush") as flush, patch.object(db, "commit") as commit, \
                        patch.object(db, "rollback") as rollback:
                    self.assertEqual(repos.ConversationRepo(db).recent_messages(self.cid), [])
                    self.assertEqual(repos.ChapterRepo(db).recent_finalized_summaries(self.nid), [])
                    flush.assert_not_called()
                    commit.assert_not_called()
                    rollback.assert_not_called()
                self.assertIn(message, db.new)
                self.assertIn(chapter, db.new)
                db.rollback()

    def test_dirty_default_autoflush_values_are_visible_and_caller_can_rollback(self):
        self.seed(messages=1, chapters=1)
        with self.sessions() as db:
            message = db.query(models.Message).filter_by(conversation_id=self.cid).one()
            chapter = db.query(models.Chapter).filter_by(novel_id=self.nid).one()
            message.content, chapter.summary = "作者待提交消息", "作者待提交摘要"
            self.assertEqual(repos.ConversationRepo(db).recent_messages(self.cid)[0].content, "作者待提交消息")
            self.assertEqual(repos.ChapterRepo(db).recent_finalized_summaries(self.nid)[0].summary, "作者待提交摘要")
            db.rollback()
        with self.sessions() as db:
            self.assertEqual(repos.ConversationRepo(db).recent_messages(self.cid)[0].content, "消息0😀\x00")

    def test_dirty_no_autoflush_reads_actual_columns_and_keeps_author_edits(self):
        self.seed(messages=1, chapters=1)
        with self.sessions() as db:
            message = db.query(models.Message).filter_by(conversation_id=self.cid).one()
            chapter = db.query(models.Chapter).filter_by(novel_id=self.nid).one()
            message.content, chapter.summary = "本地未保存消息", "本地未保存摘要"
            with db.no_autoflush, patch.object(db, "flush") as flush, patch.object(db, "commit") as commit, \
                    patch.object(db, "rollback") as rollback:
                # 原完整ORM查询显示本地缓存；新上下文只读取当前真实事务列，不叠回未保存编辑。
                self.assertEqual(repos.ConversationRepo(db).list_messages(self.cid)[0].content, "本地未保存消息")
                self.assertEqual(repos.ConversationRepo(db).recent_messages(self.cid)[0].content, "消息0😀\x00")
                self.assertEqual(repos.ChapterRepo(db).recent_finalized_summaries(self.nid)[0].summary, "摘要0😀\x00")
                flush.assert_not_called()
                commit.assert_not_called()
                rollback.assert_not_called()
            self.assertIn(message, db.dirty)
            self.assertIn(chapter, db.dirty)
            db.commit()
        with self.sessions() as db:
            self.assertEqual(repos.ConversationRepo(db).recent_messages(self.cid)[0].content, "本地未保存消息")
            self.assertEqual(repos.ChapterRepo(db).recent_finalized_summaries(self.nid)[0].summary, "本地未保存摘要")

    def test_stale_clean_orm_cache_does_not_mask_fresh_columns(self):
        self.seed(messages=1, chapters=1)
        with self.sessions(autoflush=False) as db:
            cached_message = db.query(models.Message).filter_by(conversation_id=self.cid).one()
            cached_chapter = db.query(models.Chapter).filter_by(novel_id=self.nid).one()
            with self.sessions() as writer:
                writer.execute(models.Message.__table__.update().where(models.Message.id == cached_message.id)
                    .values(content="另一窗口消息"))
                writer.execute(models.Chapter.__table__.update().where(models.Chapter.id == cached_chapter.id)
                    .values(summary="另一窗口摘要"))
                writer.commit()
            self.assertEqual(repos.ConversationRepo(db).recent_messages(self.cid)[0].content, "另一窗口消息")
            self.assertEqual(repos.ChapterRepo(db).recent_finalized_summaries(self.nid)[0].summary, "另一窗口摘要")
            self.assertEqual(cached_message.content, "消息0😀\x00")
            self.assertEqual(cached_chapter.summary, "摘要0😀\x00")

    def test_flushed_and_core_writes_remain_owned_by_caller(self):
        self.seed(messages=1, chapters=1)
        with self.sessions() as db:
            chapter = db.query(models.Chapter).filter_by(novel_id=self.nid).one()
            chapter.summary = "已flush摘要"
            db.flush()
            db.execute(text("UPDATE message SET content=:value WHERE conversation_id=:cid"),
                {"value": "Core未提交消息", "cid": self.cid})
            with patch.object(db, "commit") as commit, patch.object(db, "rollback") as rollback:
                self.assertEqual(repos.ConversationRepo(db).recent_messages(self.cid)[0].content, "Core未提交消息")
                self.assertEqual(repos.ChapterRepo(db).recent_finalized_summaries(self.nid)[0].summary, "已flush摘要")
                commit.assert_not_called()
                rollback.assert_not_called()
            db.rollback()
        with self.sessions() as db:
            self.assertEqual(repos.ConversationRepo(db).recent_messages(self.cid)[0].content, "消息0😀\x00")
            self.assertEqual(repos.ChapterRepo(db).recent_finalized_summaries(self.nid)[0].summary, "摘要0😀\x00")

    def test_native_reply_uses_twenty_rows_without_full_history_or_chapters(self):
        self.seed(messages=50, chapters=8, tied=True)
        for keyed in (False, True):
            with self.subTest(keyed=keyed), self.sessions() as db:
                with self.sessions() as oracle:
                    before = self.values(repos.ConversationRepo(oracle).list_messages(self.cid), ("role", "content"))
                    recent = repos.ChapterRepo(oracle).finalized(self.nid)[-3:]
                    summary = "\n\n【最近章节】\n" + "\n".join(f"第{row.seq}章：{row.summary}" for row in recent)
                expected = [{"role": "system", "content": chat.CHAT_ADVISOR_SYSTEM},
                    {"role": "system", "content": "当前作品设定：\n" + summary}]
                expected += [{"role": role, "content": content} for role, content in (before + [("user", "精确新消息")])[-20:]]
                with patch.object(repos.ConversationRepo, "list_messages", side_effect=AssertionError("不能读全历史")), \
                        patch.object(repos.ChapterRepo, "finalized", side_effect=AssertionError("不能读全本正文")), \
                        patch("app.infrastructure.vector.search", return_value=[]), \
                        patch.object(chat.llm, "stream_complete", return_value=iter(["回复", "😀"])) as provider:
                    kwargs = {"request_id": str(uuid4())} if keyed else {}
                    frames = [json.loads(line[5:]) for line in chat.reply_conversation(db, self.cid, "精确新消息", **kwargs)]
                self.assertEqual(provider.call_args.args[0], expected)
                self.assertEqual([row["type"] for row in frames], (["user_saved"] if keyed else []) + ["token", "token", "done"])
                self.assertEqual(frames[-1]["user_message_id"], kwargs.get("request_id", frames[-1]["user_message_id"]))

    def test_full_history_and_finalized_keep_all_rows_columns_and_meta(self):
        self.seed(messages=50, chapters=8, tied=True)
        with self.sessions() as db:
            messages = repos.ConversationRepo(db).list_messages(self.cid)
            chapters = repos.ChapterRepo(db).finalized(self.nid)
            self.assertEqual((len(messages), len(chapters)), (50, 8))
            self.assertEqual([row.meta for row in messages], [{"original": i} for i in range(50)])
            self.assertTrue(all(isinstance(row, models.Message) for row in messages))
            self.assertTrue(all(isinstance(row, models.Chapter) and len(row.content) == 6000 for row in chapters))
            self.assertEqual(len(db.execute(select(models.Message.__table__)).first()), 6)

    def test_context_budget_and_story_bible_section_order_remain_unchanged(self):
        self.seed(chapters=4)
        with self.sessions() as db:
            db.add(models.Character(novel_id=self.nid, name="林舟", role="主角", personality="谨慎", goal="归家"))
            db.add(models.WorldSetting(novel_id=self.nid, name="灯城", category="地点", description="长明灯"))
            db.add(models.OutlineNode(novel_id=self.nid, title="归途", level="volume"))
            db.commit()
            with patch("app.infrastructure.vector.search", return_value=[{"text": "原召回事实", "kind": "setting"}]):
                context = chat.build_chat_context(db, self.nid, query="灯城", max_chars=2000)
                limited = chat.build_chat_context(db, self.nid, query="灯城", max_chars=30)
            headings = ["【人物·当前设定】", "【语义召回·仅作参考】", "【世界观·当前设定】", "【大纲】", "【最近章节】"]
            self.assertEqual(sorted(headings, key=context.index), headings)
            self.assertTrue(context.startswith(limited))
            self.assertLessEqual(len(limited), 30)
            self.assertIn("林舟", limited)
            self.assertNotIn("第1章", context)


if __name__ == "__main__":
    unittest.main()
