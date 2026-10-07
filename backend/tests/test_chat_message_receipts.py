"""原次聊天发送确认：真实HTTP/SSE、SQLite事务、作者原文精确身份。"""
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Thread
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from app.application import chat
from app.infrastructure import models, repository as repos
from app.infrastructure.db import get_db
from app.interfaces.api.v1 import conversations
from app.interfaces import schemas


def frames(body):
    return [json.loads(line[5:].strip()) for line in body.splitlines() if line.startswith("data:")]


class ChatMessageReceiptTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "chat-receipt.db"
        self.engine = create_engine(f"sqlite:///{self.path.as_posix()}",
            connect_args={"check_same_thread": False, "timeout": .1})
        self.addCleanup(self.engine.dispose)
        models.Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        with self.sessions() as db:
            self.cid = repos.ConversationRepo(db).create().id
            self.other = repos.ConversationRepo(db).create(title="另一会话").id
            repos.ConversationRepo(db).add_message(self.other, "user", "另一会话原构思")
        self.app = FastAPI()
        self.app.include_router(conversations.router, prefix="/api/v1")

        def db():
            with self.sessions() as session:
                yield session
        self.app.dependency_overrides[get_db] = db
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        mocked = patch.object(conversations, "SessionLocal", self.sessions)
        mocked.start()
        self.addCleanup(mocked.stop)
        self.base = f"/api/v1/conversations/{self.cid}/messages"
        self.request_id = str(uuid4())
        self.original = " \x00作者精确构思😀：母亲仍在城内，灯号揭示她的行踪。\r\n "
        self.reply = "她举起灯，等待下一次讨论。"

    def _send(self, content=None, request_id=None, base=None):
        with patch.object(chat.llm, "stream_complete", return_value=iter([self.reply])) as provider:
            response = self.client.post(base or self.base, json={"content": self.original if content is None else content,
                "request_id": self.request_id if request_id is None else request_id})
        return response, provider.call_count

    def _receipt(self, request_id=None, base=None):
        return self.client.get(f"{base or self.base}/requests/{request_id or self.request_id}")

    def _rows(self):
        with self.sessions() as db:
            return {table.name: [dict(row) for row in db.execute(select(table)).mappings()]
                    for table in models.Base.metadata.sorted_tables}

    def _assert_receipt(self, body, replayed):
        self.assertEqual({key: body[key] for key in ("request_id", "conversation_id", "user_message_id", "content",
            "content_hash", "accepted", "receipt_replayed")}, {
            "request_id": self.request_id, "conversation_id": self.cid, "user_message_id": self.request_id,
            "content": self.original, "content_hash": hashlib.sha256(self.original.encode("utf-8")).hexdigest(),
            "accepted": True, "receipt_replayed": replayed})

    def test_first_send_precise_saved_ack_and_new_done_identity(self):
        before = self._rows()
        response, count = self._send(request_id=self.request_id.upper())
        self.assertEqual((response.status_code, count), (200, 1), response.text)
        values = frames(response.text)
        self.assertEqual([row["type"] for row in values], ["user_saved", "token", "done"])
        self._assert_receipt(values[0], False)
        self.assertEqual((values[-1]["request_id"], values[-1]["conversation_id"], values[-1]["user_message_id"]),
                         (self.request_id, self.cid, self.request_id))
        current = self.client.get(self.base).json()
        self.assertEqual([row["role"] for row in current], ["user", "assistant"])
        self.assertEqual(current[0]["content"], self.original)
        self.assertEqual(current[1]["id"], values[-1]["assistant_message_id"])
        self.assertEqual(current[1]["content"], self.reply)
        other_before = [row for row in before["message"] if row["conversation_id"] == self.other]
        self.assertEqual(other_before, [row for row in self._rows()["message"] if row["conversation_id"] == self.other])

    def test_saved_ack_reaches_actual_asgi_send_before_model_starts(self):
        emitted = []
        app = self.app

        async def captured(scope, receive, send):
            async def capture(message):
                if message["type"] == "http.response.body":
                    emitted.append(message.get("body", b""))
                await send(message)
            await app(scope, receive, capture)

        def provider(*_args, **_kwargs):
            self.assertTrue(any(b'"type": "user_saved"' in piece for piece in emitted))
            with self.sessions() as observer:
                self.assertEqual(observer.get(models.Message, self.request_id).content, self.original)
            yield self.reply
        with TestClient(captured) as client, patch.object(chat.llm, "stream_complete", side_effect=provider):
            response = client.post(self.base, json={"content": self.original, "request_id": self.request_id})
        self.assertEqual(frames(response.text)[-1]["type"], "done")

    def test_same_request_replay_only_receipt_no_model_or_mutation(self):
        self._send()
        before = self._rows()
        response, count = self._send()
        self.assertEqual(count, 0)
        values = frames(response.text)
        self.assertEqual([row["type"] for row in values], ["receipt"])
        self._assert_receipt(values[0], True)
        self.assertIs(values[0]["reply_restarted"], False)
        self.assertEqual(self._rows(), before)

    def test_same_text_new_uuid_remains_legal_new_turn(self):
        self._send()
        response, count = self._send(request_id=str(uuid4()))
        self.assertEqual((count, frames(response.text)[-1]["type"]), (1, "done"))
        self.assertEqual(len(self.client.get(self.base).json()), 4)

    def test_get_receipt_survives_new_session_and_meta_changes_without_reply_claim(self):
        self._send()
        self.client.patch(f"{self.base}/{self.request_id}", json={"meta": {"accepted": False, "content_hash": "fake"}})
        before = self._rows()
        response = self._receipt(request_id=self.request_id.upper())
        self.assertEqual(response.status_code, 200)
        self._assert_receipt(response.json(), True)
        self.assertNotIn("assistant_message_id", response.json())
        self.assertNotIn("reply_complete", response.json())
        self.assertEqual(self._rows(), before)

    def test_unknown_key_and_deleted_conversation_have_distinct_404_no_model(self):
        missing = self._receipt()
        self.assertEqual((missing.status_code, missing.json()["code"]), (404, "chat_message_request_not_found"))
        self.client.delete(f"/api/v1/conversations/{self.cid}")
        missing_conv = self._receipt()
        self.assertEqual((missing_conv.status_code, missing_conv.json()["code"]), (404, "chat_conversation_not_found"))
        response, count = self._send()
        self.assertEqual(count, 0)
        self.assertEqual((frames(response.text)[-1]["status"], frames(response.text)[-1]["code"]),
                         (404, "chat_conversation_not_found"))

    def test_payload_and_scope_conflicts_do_not_disclose_original_or_write(self):
        self._send()
        before = self._rows()
        for data in ({"content": "不同作者内容"}, {"base": f"/api/v1/conversations/{self.other}/messages"}):
            with self.subTest(data=data):
                response, count = self._send(**data)
                error = frames(response.text)[-1]
                self.assertEqual((count, error["status"], error["code"]), (0, 409, "chat_message_request_conflict"))
                self.assertNotIn(self.original, error["detail"])
                self.assertEqual(self._rows(), before)
        foreign = self._receipt(base=f"/api/v1/conversations/{self.other}/messages")
        self.assertEqual((foreign.status_code, foreign.json()["code"]), (409, "chat_message_request_conflict"))
        self.assertNotIn(self.original, foreign.text)

    def test_assistant_id_cannot_be_used_as_user_receipt_or_send_identity(self):
        with self.sessions() as db:
            db.add(models.Message(id=self.request_id, conversation_id=self.cid, role="assistant", content="私有旧助手原文"))
            db.commit()
        response = self._receipt()
        self.assertEqual((response.status_code, response.json()["code"]), (409, "chat_message_request_conflict"))
        before = self._rows()
        response, count = self._send()
        error = frames(response.text)[-1]
        self.assertEqual((count, error["status"], error["code"]), (0, 409, "chat_message_request_conflict"))
        self.assertNotIn("私有旧助手原文", error["detail"])
        self.assertEqual(self._rows(), before)

    def test_invalid_uuid_is_422_before_model_or_write(self):
        before = self._rows()
        with patch.object(chat.llm, "stream_complete") as provider:
            for request_id in (True, "not-a-uuid", 123, {}):
                self.assertEqual(self.client.post(self.base, json={"content": self.original, "request_id": request_id}).status_code, 422)
            self.assertEqual(self._receipt(request_id="not-a-uuid").status_code, 422)
            provider.assert_not_called()
        self.assertEqual(self._rows(), before)

    def test_true_trigger_rejects_user_and_auto_title_then_same_uuid_retry_can_succeed(self):
        before = self._rows()
        with self.engine.begin() as db:
            db.exec_driver_sql("""CREATE TRIGGER reject_chat_user BEFORE INSERT ON message WHEN NEW.role='user'
                BEGIN SELECT RAISE(ABORT,'controlled user rejection'); END""")
        failed, count = self._send()
        self.assertEqual((count, [row["type"] for row in frames(failed.text)]), (0, ["error"]))
        self.assertEqual(self._rows(), before)
        self.assertEqual(self._receipt().json()["code"], "chat_message_request_not_found")
        with self.engine.begin() as db:
            db.exec_driver_sql("DROP TRIGGER reject_chat_user")
        response, count = self._send()
        self.assertEqual((count, frames(response.text)[-1]["type"]), (1, "done"))

    def test_true_assistant_trigger_failure_keeps_user_ack_and_does_not_retry_generation(self):
        with self.engine.begin() as db:
            db.exec_driver_sql("""CREATE TRIGGER reject_chat_assistant BEFORE INSERT ON message WHEN NEW.role='assistant'
                BEGIN SELECT RAISE(ABORT,'controlled assistant rejection'); END""")
        response, count = self._send()
        values = frames(response.text)
        self.assertEqual((count, [row["type"] for row in values]), (1, ["user_saved", "token", "error"]))
        self._assert_receipt(values[0], False)
        self.assertEqual(len(self.client.get(self.base).json()), 1)
        before = self._rows()
        replay, count = self._send()
        self.assertEqual((count, frames(replay.text)[-1]["type"]), (0, "receipt"))
        self.assertEqual(self._rows(), before)

    def test_actual_user_and_assistant_database_failures_hide_sql_and_raw_parameters(self):
        expected = "本次消息保存或读取未完成，请保留原稿并核对原发送；回复是否完整请核对历史消息"
        for role in ("user", "assistant"):
            with self.subTest(role=role):
                with self.engine.begin() as db:
                    db.exec_driver_sql(f"""CREATE TRIGGER reject_chat_database BEFORE INSERT ON message
                        WHEN NEW.role='{role}' BEGIN SELECT RAISE(ABORT,'controlled private database detail'); END""")
                response, count = self._send()
                values = frames(response.text)
                error = values[-1]
                self.assertEqual(error["type"], "error")
                self.assertEqual((error["code"], error["status"]), ("chat_generation_failed", 500))
                self.assertEqual(error["detail"], expected)
                for private in ("INSERT INTO", "parameters", "sqlite3", "sqlalche.me", "作者精确构思",
                                "controlled private database detail"):
                    self.assertNotIn(private, error["detail"])
                receipt = self._receipt()
                messages = self.client.get(self.base).json()
                if role == "user":
                    self.assertEqual((count, len(values), receipt.status_code, messages), (0, 1, 404, []))
                else:
                    self.assertEqual((count, values[0]["type"], len(messages)), (1, "user_saved", 1))
                    self._assert_receipt(receipt.json(), True)
                    self.assertEqual(messages[0]["content"], self.original)
                with self.engine.begin() as db:
                    db.exec_driver_sql("DROP TRIGGER reject_chat_database")

    def test_provider_interruption_records_no_partial_assistant_and_replay_is_accepted_only(self):
        def provider(*_args, **_kwargs):
            yield "可复制的局部回答"
            raise RuntimeError("受控模型连接中断")
        with patch.object(chat.llm, "stream_complete", side_effect=provider):
            response = self.client.post(self.base, json={"content": self.original, "request_id": self.request_id})
        self.assertEqual([row["type"] for row in frames(response.text)], ["user_saved", "token", "error"])
        self._assert_receipt(self._receipt().json(), True)
        self.assertEqual(len(self.client.get(self.base).json()), 1)
        self.assertEqual(self._send()[1], 0)

    def test_true_two_connections_same_uuid_only_one_model_and_no_lock_during_wait(self):
        started, release, barrier = Event(), Event(), Barrier(3)
        results, errors, calls = [], [], []

        def provider(*_args, **_kwargs):
            calls.append(1)
            started.set()
            if not release.wait(5):
                raise RuntimeError("模型门闩超时")
            yield self.reply

        def request():
            try:
                barrier.wait(timeout=3)
                results.append(self.client.post(self.base, json={"content": self.original, "request_id": self.request_id}))
            except Exception as error:
                errors.append(error)
        with patch.object(chat.llm, "stream_complete", side_effect=provider):
            workers = [Thread(target=request) for _ in range(2)]
            for worker in workers:
                worker.start()
            barrier.wait(timeout=3)
            try:
                self.assertTrue(started.wait(3))
                changed = self.client.patch(f"/api/v1/conversations/{self.cid}", json={"title": "模型等待时另一请求修改标题"})
                self.assertEqual(changed.status_code, 200, changed.text)
            finally:
                release.set()
                for worker in workers:
                    worker.join(timeout=5)
                    self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(sorted(frames(row.text)[-1]["type"] for row in results), ["done", "receipt"])
        self.assertEqual(len(self.client.get(self.base).json()), 2)

    def test_saved_user_then_process_gap_replay_cannot_restart_reply(self):
        with self.sessions() as db:
            generator = chat.reply_conversation(db, self.cid, self.original, request_id=self.request_id)
            with patch.object(chat.llm, "stream_complete") as provider:
                self._assert_receipt(frames(next(generator))[0], False)
                provider.assert_not_called()
                generator.close()  # 已提交user后、调用模型前中止当前执行。
        before = self._rows()
        response, count = self._send()
        self.assertEqual((count, frames(response.text)[-1]["type"]), (0, "receipt"))
        self.assertEqual(self._rows(), before)

    def test_keyed_send_refuses_caller_pending_flushed_core_deleted_before_touching_transaction(self):
        for mode in ("new", "dirty", "flushed", "core", "deleted"):
            with self.subTest(mode=mode), self.sessions(autoflush=True) as db:
                if mode in ("new", "flushed"):
                    db.add(models.Conversation(title="调用方新会话"))
                    if mode == "flushed":
                        db.flush()
                elif mode == "core":
                    db.execute(text("UPDATE conversation SET title='调用方未提交' WHERE id=:id"), {"id": self.other})
                else:
                    current = repos.ConversationRepo(db).get(self.other)
                    if mode == "dirty":
                        current.title = "调用方尚未flush"
                    else:
                        db.delete(current)
                with patch.object(db, "flush", wraps=db.flush) as flush, patch.object(db, "commit", wraps=db.commit) as commit, \
                        patch.object(db, "rollback", wraps=db.rollback) as rollback, patch.object(chat.llm, "stream_complete") as provider:
                    values = frames("".join(chat.reply_conversation(db, self.cid, self.original, request_id=self.request_id)))
                    self.assertEqual(values[-1]["type"], "error")
                    flush.assert_not_called()
                    commit.assert_not_called()
                    rollback.assert_not_called()
                    provider.assert_not_called()
                self.assertTrue(db.in_transaction())
                db.rollback()

    def test_get_receipt_keeps_caller_new_flushed_core_without_commit_rollback_or_autoflush(self):
        self._send()
        for mode in ("new", "flushed", "core"):
            with self.subTest(mode=mode), self.sessions(autoflush=True) as db:
                if mode == "core":
                    db.execute(text("UPDATE conversation SET title='调用方未提交' WHERE id=:id"), {"id": self.other})
                else:
                    db.add(models.Conversation(title="调用方未提交新会话"))
                    if mode == "flushed":
                        db.flush()
                with patch.object(db, "flush", wraps=db.flush) as flush, patch.object(db, "commit", wraps=db.commit) as commit, \
                        patch.object(db, "rollback", wraps=db.rollback) as rollback:
                    self._assert_receipt(chat.get_message_receipt(db, self.cid, self.request_id), True)
                    flush.assert_not_called()
                    commit.assert_not_called()
                    rollback.assert_not_called()
                self.assertTrue(db.in_transaction())
                db.rollback()

    def test_shared_add_message_preserves_existing_caller_commit_and_same_identity(self):
        with self.sessions() as db:
            pending = models.Conversation(title="调用方待一起保存")
            db.add(pending)
            db.execute(text("UPDATE conversation SET title='调用方Core排序等写入' WHERE id=:id"), {"id": self.other})
            saved = repos.ConversationRepo(db).add_message(self.cid, "user", "共享仓储旧调用", meta={"nested": [1, "原稿"]})
            self.assertIs(saved, db.get(models.Message, saved.id))
            self.assertFalse(db.dirty)
            with self.sessions() as observer:
                self.assertIsNotNone(observer.get(models.Conversation, pending.id))
                self.assertEqual(observer.get(models.Conversation, self.other).title, "调用方Core排序等写入")
            self.assertEqual(schemas.MessageRead.model_validate(saved).content, "共享仓储旧调用")

    def test_shared_commit_false_flushes_into_caller_transaction_and_all_can_rollback(self):
        before = self._rows()
        with self.sessions() as db:
            db.execute(text("UPDATE conversation SET title='调用方Core未提交' WHERE id=:id"), {"id": self.other})
            saved = repos.ConversationRepo(db).add_message(self.cid, "user", "调用方消息", commit=False)
            self.assertIsNotNone(saved.id)
            self.assertTrue(db.in_transaction())
            db.rollback()
        self.assertEqual(self._rows(), before)

    def test_shared_append_real_postcommit_exclusive_has_zero_sql_and_all_six_columns(self):
        original_commit = self.sessions.class_.commit
        blockers, statements = [], []
        committed = Event()

        def locked_commit(db):
            original_commit(db)
            blocker = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
            blocker.execute("BEGIN EXCLUSIVE")
            blockers.append(blocker)
            committed.set()

        def sql(_connection, _cursor, statement, *_args):
            if committed.is_set():
                statements.append(statement)
        event.listen(self.engine, "before_cursor_execute", sql)
        try:
            with self.sessions() as db, patch.object(self.sessions.class_, "commit", new=locked_commit):
                saved = repos.ConversationRepo(db).add_message(self.cid, "user", self.original, meta={"original": True})
                result = schemas.MessageRead.model_validate(saved).model_dump()
                self.assertEqual(set(result), {column.key for column in models.Message.__table__.columns})
                self.assertEqual(result["content"], self.original)
                self.assertEqual(statements, [])
                self.assertFalse(db.dirty)
        finally:
            for blocker in blockers:
                blocker.rollback()
                blocker.close()
            event.remove(self.engine, "before_cursor_execute", sql)

    def test_assistant_to_done_real_postcommit_exclusive_has_zero_sql_for_keyed_and_legacy(self):
        for keyed in (True, False):
            with self.subTest(keyed=keyed):
                original_commit = self.sessions.class_.commit
                blockers, statements = [], []
                committed = Event()

                def on_flush(db, *_args):
                    roles = [row.role for row in db.new if isinstance(row, models.Message)]
                    if roles:
                        db.info["latest_message_role"] = roles[-1]

                def locked_commit(db):
                    original_commit(db)
                    if db.info.get("latest_message_role") == "assistant":
                        blocker = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
                        blocker.execute("BEGIN EXCLUSIVE")
                        blockers.append(blocker)
                        committed.set()

                def sql(_connection, _cursor, statement, *_args):
                    if committed.is_set():
                        statements.append(statement)
                event.listen(self.sessions.class_, "before_flush", on_flush)
                event.listen(self.engine, "before_cursor_execute", sql)
                try:
                    payload = {"content": self.original}
                    if keyed:
                        payload["request_id"] = self.request_id
                    with patch.object(self.sessions.class_, "commit", new=locked_commit), \
                            patch.object(chat.llm, "stream_complete", return_value=iter([self.reply])):
                        response = self.client.post(self.base, json=payload)
                    self.assertEqual(frames(response.text)[-1]["type"], "done", response.text)
                    self.assertEqual(statements, [])
                finally:
                    for blocker in blockers:
                        blocker.rollback()
                        blocker.close()
                    event.remove(self.sessions.class_, "before_flush", on_flush)
                    event.remove(self.engine, "before_cursor_execute", sql)

    def test_legacy_no_uuid_keeps_old_token_done_shape(self):
        with patch.object(chat.llm, "stream_complete", return_value=iter([self.reply])):
            response = self.client.post(self.base, json={"content": self.original})
        values = frames(response.text)
        self.assertEqual([row["type"] for row in values], ["token", "done"])
        self.assertEqual(set(values[-1]), {"type", "user_message_id", "assistant_message_id", "title"})


if __name__ == "__main__":
    unittest.main()
