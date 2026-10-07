"""真实HTTP/SQLite聊天窗口与批量动作：旧响应、游标、隔离及读事务。"""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from app.application import chat, chat_actions
from app.infrastructure import models, repository as repos
from app.infrastructure.db import get_db
from app.interfaces.api.v1 import conversations


class ChatHistoryWindowTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.engine = create_engine(f"sqlite:///{Path(temporary.name).as_posix()}/history.db",
            connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        models.Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        with self.sessions() as db:
            novel = models.Novel(id="work", title="本作品")
            foreign = models.Novel(id="foreign-work", title="外部作品")
            db.add_all([novel, foreign])
            db.add_all([models.Conversation(id="thread", novel_id="work", title="本会话"),
                models.Conversation(id="other-thread", novel_id="foreign-work", title="外部会话"),
                models.Conversation(id="global-thread", title="全局会话")])
            db.commit()
        self.base = "/api/v1/conversations/thread/messages"
        self.app = FastAPI()
        self.app.include_router(conversations.router, prefix="/api/v1")

        def dependency():
            with self.sessions() as db:
                yield db

        self.app.dependency_overrides[get_db] = dependency
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def seed(self, count, *, all_assistant=False):
        with self.sessions() as db:
            db.add_all([models.Message(id=f"message-{i}", conversation_id="thread",
                role="assistant" if all_assistant or i % 2 else "user", content=f"原消息{i}😀\x00 \r\n",
                meta=None if i % 3 else {"applied": [{"id": f"实体{i}"}], "keep": [i]},
                # Deliberate ties spanning page boundaries and non-UUID legacy IDs.
                created_at=f"2026-01-01T00:00:00.{i // 9:06}") for i in range(count)])
            db.add(models.Message(id="foreign-source", conversation_id="other-thread", role="assistant",
                content="外国秘密原文", created_at="2099"))
            db.add(models.Message(id="global-source", conversation_id="global-thread", role="assistant", content="全局构思"))
            db.commit()

    def window(self, **query):
        return self.client.get(self.base + "/window", params=query)

    def batch(self, ids):
        return self.client.post(self.base + "/actions/batch", json={"message_ids": ids})

    def rows(self):
        with self.sessions() as db:
            return {table.name: [dict(row) for row in db.execute(select(table)).mappings()]
                for table in models.Base.metadata.sorted_tables}

    def test_default_window_is_exact_last_fifty_full_old_messages(self):
        self.seed(103)
        full = self.client.get(self.base).json()
        response = self.window()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(set(result), {"conversation_id", "novel_id", "items", "has_more", "next_before_message_id"})
        self.assertEqual(result, {"conversation_id": "thread", "novel_id": "work", "items": full[-50:],
            "has_more": True, "next_before_message_id": full[-50]["id"]})
        self.assertEqual(set(result["items"][0]), {"id", "conversation_id", "role", "content", "meta", "created_at"})

    def test_complete_pagination_matches_full_api_with_timestamp_ties(self):
        self.seed(103)
        expected = self.client.get(self.base).json()
        for limit in (1, 7, 50, 100):
            with self.subTest(limit=limit):
                pages, cursor = [], None
                while True:
                    query = {"limit": limit}
                    if cursor is not None:
                        query["before_message_id"] = cursor
                    response = self.window(**query)
                    self.assertEqual(response.status_code, 200, response.text)
                    body = response.json()
                    pages[0:0] = body["items"]
                    if not body["has_more"]:
                        self.assertIsNone(body["next_before_message_id"])
                        break
                    self.assertEqual(body["next_before_message_id"], body["items"][0]["id"])
                    cursor = body["next_before_message_id"]
                self.assertEqual(pages, expected)
                self.assertEqual(len({row["id"] for row in pages}), 103)

    def test_newer_append_does_not_move_existing_older_page_boundary(self):
        self.seed(60)
        original = self.client.get(self.base).json()
        first = self.window(limit=20).json()
        with self.sessions() as db:
            db.add(models.Message(id="newer", conversation_id="thread", role="assistant", content="新回复", created_at="2099"))
            db.commit()
        older = self.window(limit=100, before_message_id=first["next_before_message_id"]).json()
        self.assertEqual(older["items"] + first["items"], original)
        self.assertEqual(self.window(limit=20).json()["items"][-1]["id"], "newer")

    def test_empty_and_global_windows_and_global_batch(self):
        result = self.window().json()
        self.assertEqual(result, {"conversation_id": "thread", "novel_id": "work", "items": [],
            "has_more": False, "next_before_message_id": None})
        self.seed(1)
        global_base = "/api/v1/conversations/global-thread/messages"
        window = self.client.get(global_base + "/window").json()
        self.assertIsNone(window["novel_id"])
        self.assertEqual(window["items"][0]["id"], "global-source")
        response = self.client.post(global_base + "/actions/batch", json={"message_ids": ["global-source"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"conversation_id": "global-thread", "novel_id": None,
            "actions_by_message_id": {"global-source": []}})
        self.assertEqual(self.client.get(global_base + "/global-source/actions").status_code, 400)

    def test_invalid_limits_and_empty_cursor_are_422(self):
        for query in ({"limit": 0}, {"limit": -1}, {"limit": 101}, {"limit": "true"}, {"limit": "no"},
                      {"before_message_id": ""}):
            with self.subTest(query=query):
                self.assertEqual(self.window(**query).status_code, 422)

    def test_missing_or_foreign_cursor_and_missing_scope_are_404_without_leak(self):
        self.seed(10)
        for cursor in ("missing", "foreign-source", "global-source"):
            with self.subTest(cursor=cursor):
                response = self.window(before_message_id=cursor)
                self.assertEqual(response.status_code, 404, response.text)
                self.assertEqual(response.json()["code"], "chat_message_cursor_not_found")
                self.assertNotIn("秘密", response.text)
        response = self.client.get("/api/v1/conversations/missing/messages/window")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "chat_conversation_not_found")

    def test_batch_equals_old_actions_with_deleted_and_foreign_targets(self):
        self.seed(5, all_assistant=True)
        with self.sessions() as db:
            db.add(models.OutlineNode(id="present-outline", novel_id="work", title="卷", level="volume"))
            db.add(models.OutlineNode(id="foreign-outline", novel_id="foreign-work", title="外国秘密", level="volume"))
            db.add(models.Chapter(id="present-chapter", novel_id="work", seq=1, title="章"))
            db.add(models.Chapter(id="foreign-chapter", novel_id="foreign-work", seq=1, title="外国秘密"))
            rows = [("message-0", "outline", "present-outline"), ("message-0", "chapter", "missing"),
                ("message-1", "chapter", "present-chapter"), ("message-2", "outline", "foreign-outline"),
                ("message-3", "chapter", "foreign-chapter"), ("message-3", "settings", "gone-setting")]
            for index, (mid, kind, target) in enumerate(rows):
                db.add(models.ChatAction(message_id=mid, novel_id="work", action_type=kind, result_id=target,
                    result_summary={"original": index, "raw": "摘要😀\x00", "nested": [1, {"keep": True}]},
                    created_at="2026-01-01"))
            # Corrupt wrong-scope records must not contaminate matching message ID.
            db.add(models.ChatAction(message_id="message-4", novel_id="foreign-work", action_type="settings",
                result_summary={"secret": "外国动作秘密"}))
            db.commit()
        ids = [f"message-{i}" for i in range(5)]
        expected = {mid: self.client.get(f"{self.base}/{mid}/actions").json() for mid in ids}
        response = self.batch(ids)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"conversation_id": "thread", "novel_id": "work", "actions_by_message_id": expected})
        self.assertNotIn("外国动作秘密", response.text)
        self.assertEqual(expected["message-4"], [])

    def test_batch_mixed_invalid_sources_fails_whole_request_without_partial_leak(self):
        self.seed(5)
        for ids in (["message-1", "message-0"], ["message-1", "foreign-source"], ["message-1", "missing"]):
            with self.subTest(ids=ids):
                response = self.batch(ids)
                self.assertEqual(response.status_code, 404, response.text)
                self.assertEqual(response.json()["code"], "chat_action_sources_not_found")
                self.assertNotIn("actions_by_message_id", response.json())
                self.assertNotIn("秘密", response.text)

    def test_batch_request_limits_duplicates_and_nonstring_ids_are_422(self):
        for ids in ([], ["same", "same"], [""], [None], [False], [1], [str(i) for i in range(101)]):
            with self.subTest(ids=ids):
                self.assertEqual(self.batch(ids).status_code, 422)

    def test_window_selects_bounded_six_columns_without_message_orm_load(self):
        self.seed(150)
        selected, loaded = [], []

        def cursor(_conn, cursor, statement, *_args):
            if statement.startswith("SELECT") and "FROM message" in statement:
                selected.append(([row[0] for row in cursor.description], statement))

        event.listen(self.engine, "after_cursor_execute", cursor)
        self.addCleanup(event.remove, self.engine, "after_cursor_execute", cursor)
        with self.sessions() as db:
            event.listen(db, "loaded_as_persistent", lambda _session, obj: loaded.append(type(obj)))
            body = repos.ConversationRepo(db).list_message_window("thread", limit=50)
            self.assertEqual(len(body["items"]), 50)
        self.assertEqual(len(selected), 1)
        self.assertEqual(len(selected[0][0]), 6)
        self.assertIn("LIMIT", selected[0][1])
        self.assertNotIn(models.Message, loaded)

    def test_hundred_source_batch_has_five_selects_and_no_target_orm(self):
        self.seed(100, all_assistant=True)
        with self.sessions() as db:
            for i in range(100):
                db.add(models.OutlineNode(id=f"outline-{i}", novel_id="work", title="卷", level="volume"))
                db.add(models.Chapter(id=f"chapter-{i}", novel_id="work", seq=i + 1))
                for kind in ("outline", "chapter"):
                    db.add(models.ChatAction(message_id=f"message-{i}", novel_id="work", action_type=kind,
                        result_id=f"{kind}-{i}", result_summary={"id": i}))
            db.commit()
        statements, loaded = [], []

        def captured(_conn, _cursor, statement, *_args):
            if statement.startswith("SELECT"):
                statements.append(statement)

        event.listen(self.engine, "after_cursor_execute", captured)
        self.addCleanup(event.remove, self.engine, "after_cursor_execute", captured)
        with self.sessions() as db:
            event.listen(db, "loaded_as_persistent", lambda _session, obj: loaded.append(type(obj)))
            result = chat_actions.list_actions_batch(db, "thread", [f"message-{i}" for i in range(100)])
        self.assertEqual(len(result["actions_by_message_id"]), 100)
        self.assertEqual(len(statements), 5)
        self.assertEqual(loaded, [])
        self.assertTrue(all(not action["target_deleted"] for actions in result["actions_by_message_id"].values() for action in actions))

    def test_default_autoflush_keeps_new_source_and_action_visible_without_commit(self):
        with self.sessions(autoflush=True) as db:
            message = models.Message(id="pending", conversation_id="thread", role="assistant", content="未提交")
            action = models.ChatAction(message_id="pending", novel_id="work", action_type="settings", result_summary={"pending": True})
            db.add_all([message, action])
            with patch.object(db, "commit") as commit, patch.object(db, "rollback") as rollback:
                self.assertEqual(repos.ConversationRepo(db).list_message_window("thread")["items"][0].content, "未提交")
                self.assertEqual(chat_actions.list_actions_batch(db, "thread", ["pending"])["actions_by_message_id"]["pending"][0]["result_summary"], {"pending": True})
                commit.assert_not_called()
                rollback.assert_not_called()
            self.assertNotIn(message, db.new)
            db.rollback()
        self.assertEqual(self.window().json()["items"], [])

    def test_no_autoflush_and_false_read_actual_columns_and_keep_dirty_edits(self):
        self.seed(1, all_assistant=True)
        for enabled in (True, False):
            with self.subTest(autoflush=enabled), self.sessions(autoflush=enabled) as db:
                message = db.get(models.Message, "message-0")
                message.content = "未保存缓存"
                db.add(models.Message(id="pending", conversation_id="thread", role="assistant", content="未flush"))
                with db.no_autoflush, patch.object(db, "flush") as flush, patch.object(db, "commit") as commit, \
                        patch.object(db, "rollback") as rollback:
                    result = repos.ConversationRepo(db).list_message_window("thread")
                    self.assertEqual(result["items"][0].content, "原消息0😀\x00 \r\n")
                    self.assertEqual(chat_actions.list_actions_batch(db, "thread", ["message-0"])["actions_by_message_id"], {"message-0": []})
                    flush.assert_not_called()
                    commit.assert_not_called()
                    rollback.assert_not_called()
                self.assertIn(message, db.dirty)
                self.assertEqual(len(db.new), 1)
                db.rollback()

    def test_flushed_core_and_deleted_target_reads_stay_in_caller_transaction(self):
        self.seed(1, all_assistant=True)
        with self.sessions() as db:
            db.add(models.Chapter(id="target", novel_id="work", seq=1))
            db.add(models.ChatAction(message_id="message-0", novel_id="work", action_type="chapter", result_id="target"))
            db.commit()
        with self.sessions() as db:
            message = db.get(models.Message, "message-0")
            message.content = "已flush未commit"
            db.flush()
            db.execute(text("DELETE FROM chapter WHERE id='target'"))
            with patch.object(db, "commit") as commit, patch.object(db, "rollback") as rollback:
                self.assertEqual(repos.ConversationRepo(db).list_message_window("thread")["items"][0].content, "已flush未commit")
                self.assertTrue(chat_actions.list_actions_batch(db, "thread", ["message-0"])["actions_by_message_id"]["message-0"][0]["target_deleted"])
                commit.assert_not_called()
                rollback.assert_not_called()
            db.rollback()
        self.assertEqual(self.window().json()["items"][0]["content"], "原消息0😀\x00 \r\n")
        self.assertFalse(self.batch(["message-0"]).json()["actions_by_message_id"]["message-0"][0]["target_deleted"])

    def test_stale_cached_source_and_scope_do_not_leak_or_mask_current_columns(self):
        self.seed(1, all_assistant=True)
        with self.sessions() as db:
            cached_conv = db.get(models.Conversation, "global-thread")
            cached_message = db.get(models.Message, "message-0")
            with self.sessions() as writer:
                writer.execute(text("UPDATE conversation SET novel_id='work' WHERE id='global-thread'"))
                writer.execute(text("UPDATE message SET content='新原文' WHERE id='message-0'"))
                writer.commit()
            self.assertEqual(repos.ConversationRepo(db).list_message_window("thread")["items"][0].content, "新原文")
            self.assertEqual(repos.ConversationRepo(db).list_message_window("global-thread")["novel_id"], "work")
            self.assertEqual(chat_actions.list_actions_batch(db, "global-thread", ["global-source"])["novel_id"], "work")
            self.assertIsNone(cached_conv.novel_id)
            self.assertEqual(cached_message.content, "原消息0😀\x00 \r\n")

    def test_reads_do_not_write_tables_or_call_models_and_old_meta_merge_still_works(self):
        self.seed(5, all_assistant=True)
        before = self.rows()
        with patch.object(chat.llm, "stream_complete", side_effect=AssertionError("no model")) as stream, \
                patch("app.application.inject_idea.llm.complete", side_effect=AssertionError("no model")) as complete:
            self.assertEqual(self.window().status_code, 200)
            self.assertEqual(self.batch(["message-0"]).status_code, 200)
            stream.assert_not_called()
            complete.assert_not_called()
        self.assertEqual(self.rows(), before)
        response = self.client.patch(self.base + "/message-0", json={"meta": {"new": True}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["meta"], {**before["message"][0]["meta"], "new": True})
        self.assertEqual(self.client.get(self.base).json()[0]["meta"], response.json()["meta"])

    def test_saved_nonce_absent_from_window_is_still_receipted_and_not_restarted(self):
        self.seed(120)
        nonce = str(uuid4())
        with self.sessions() as db:
            db.add(models.Message(id=nonce, conversation_id="thread", role="user", content="原次旧构思😀\x00", created_at="2025"))
            db.commit()
        self.assertNotIn(nonce, [row["id"] for row in self.window().json()["items"]])
        receipt = self.client.get(self.base + f"/requests/{nonce}")
        self.assertEqual(receipt.status_code, 200)
        self.assertTrue(receipt.json()["accepted"])
        with patch.object(conversations, "SessionLocal", self.sessions), \
                patch.object(chat.llm, "stream_complete") as provider:
            response = self.client.post(self.base, json={"content": "原次旧构思😀\x00", "request_id": nonce})
        provider.assert_not_called()
        self.assertIn('"type": "receipt"', response.text)
        self.assertIn('"reply_restarted": false', response.text)


if __name__ == "__main__":
    unittest.main()
