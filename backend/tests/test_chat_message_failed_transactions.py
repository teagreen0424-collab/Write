"""聊天SSE报错后不能把未提交消息留给下一合法请求。"""
import json
import sqlite3
import unittest
from unittest.mock import patch
import uuid

from fastapi import HTTPException
from app.application import chat
from app.infrastructure import models, repository as repos
from app.interfaces.api.v1 import conversations
import test_other_delete_transactions as fixtures


class ChatMessageFailedTransactionTest(unittest.TestCase):
    rows = fixtures.OtherDeleteTransactionTest.rows
    release_lock = fixtures.OtherDeleteTransactionTest.release_lock
    assert_writer_released = fixtures.OtherDeleteTransactionTest.assert_writer_released

    def setUp(self):
        fixtures.OtherDeleteTransactionTest.setUp(self)
        self.stream_calls = []
        self.stream_driver = None
        self.checkpoint = None
        def factory():
            db = self.sessions()
            driver = db.connection().connection.driver_connection
            self.stream_driver = driver
            driver.set_authorizer(self.authorizer)
            original_close = db.close
            def close():
                driver.set_authorizer(None)
                original_close()
                self.closed.append((driver.in_transaction, db.in_transaction()))
            db.close = close
            return db
        owned = patch.object(conversations, 'SessionLocal', factory)
        owned.start(); self.addCleanup(owned.stop)

    def send(self, request_id=None):
        payload = {'content': '\r\n　用户精确原稿😀\0　\n'}
        if request_id is not None:
            payload['request_id'] = request_id
        response = self.client.post('/api/v1/conversations/global/messages', json=payload)
        self.assertEqual(response.status_code, 200)
        return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]

    def assert_next_POST_only_saves_its_own_novel(self, expected):
        response = self.client.post('/api/v1/novels', json={'title': '下一合法作品😀'})
        self.assertEqual(response.status_code, 201)
        actual = self.rows()
        actual['novel'] = [r for r in actual['novel'] if r['id'] != response.json()['id']]
        self.assertEqual(actual, expected)

    def test_legacy_user_COMMIT_denial_SSE_error_no_next_request_message_leak(self):
        self.armed = True
        events = self.send()
        self.assertEqual([e['type'] for e in events], ['error'])
        self.assertEqual(self.denials, ['COMMIT'])
        self.assertEqual(self.closed[-1], (False, False))
        self.assertEqual(self.rows(), self.before)
        self.assert_writer_released()
        self.assert_next_POST_only_saves_its_own_novel(self.before)

    def test_UUID_assistant_COMMIT_denial_preserves_known_user_receipt_without_late_assistant(self):
        request_id = str(uuid.uuid4())
        def finite_stream(messages):
            self.stream_calls.append(messages)
            self.checkpoint = self.rows()
            self.armed = True
            self.stream_driver.set_authorizer(self.authorizer)
            yield '受控助手😀'
            yield '\0精确原回复\r\n'
        with patch.object(chat.llm, 'stream_complete', side_effect=finite_stream):
            events = self.send(request_id)
        self.assertEqual([e['type'] for e in events], ['user_saved', 'token', 'token', 'error'])
        self.assertEqual(events[0]['user_message_id'], request_id)
        self.assertEqual(self.denials, ['COMMIT'])
        self.assertEqual(self.closed[-1], (False, False))
        self.assertEqual(len(self.stream_calls), 1)
        self.assertEqual(self.rows(), self.checkpoint)
        self.assert_writer_released()
        self.assert_next_POST_only_saves_its_own_novel(self.checkpoint)
        receipt = self.client.get(f'/api/v1/conversations/global/messages/requests/{request_id}')
        self.assertEqual(receipt.status_code, 200)
        self.assertEqual(receipt.json()['user_message_id'], request_id)

    def test_UUID_user_commit_owned_rollback_remains_healthy_without_generation(self):
        self.armed = True
        events = self.send(str(uuid.uuid4()))
        self.assertEqual([e['type'] for e in events], ['error'])
        self.assertEqual(self.denials, ['COMMIT'])
        self.assertEqual(self.closed[-1], (False, False))
        self.assertEqual(self.rows(), self.before)
        self.assert_next_POST_only_saves_its_own_novel(self.before)

    def test_direct_commit_denial_rolls_back_shared_writes_but_commit_false_still_belongs_to_caller(self):
        with self.sessions() as db:
            db.add(models.Novel(id='pending', title='原调用方未提交'))
            driver = db.connection().connection.driver_connection
            driver.set_authorizer(self.authorizer)
            self.armed = True
            try:
                with self.assertRaises(Exception):
                    repos.ConversationRepo(db).add_message('global', 'assistant', '原稿😀\0')
                self.assertFalse(driver.in_transaction)
                self.assertFalse(db.in_transaction())
            finally:
                driver.set_authorizer(None)
        self.assertEqual(self.rows(), self.before)
        with self.sessions() as db:
            commits = self.commit_count
            pending = models.Novel(id='pending', title='调用方未提交😀')
            db.add(pending)
            message = repos.ConversationRepo(db).add_message('global', 'user', '原用户稿😀\0', commit=False)
            self.assertEqual(self.commit_count, commits)
            self.assertEqual(self.rows(), self.before)
            self.assertIs(db.get(models.Message, message.id), message)
            db.rollback()
        self.assertEqual(self.rows(), self.before)

    def test_success_same_ORM_complete6_shared_commit_and_no_postcommit_SQL(self):
        with self.sessions() as db:
            db.add(models.Novel(id='pending', title='共同保存😀'))
            db.get(models.Novel, 'foreign').title = '共同改名🐾'
            commits = self.commit_count
            self.hold_after_commit = True
            try:
                message = repos.ConversationRepo(db).add_message('global', 'assistant', '\r\n　精确成功稿😀\0　\n', meta={'source': '原meta😀'})
                self.assertEqual(self.commit_count - commits, 1)
                self.assertIsNotNone(self.lock)
                self.assertEqual(len(message.__table__.columns), 6)
                self.assertEqual(message.content, '\r\n　精确成功稿😀\0　\n')
                self.assertEqual(message.meta, {'source': '原meta😀'})
                self.assertIs(db.get(models.Message, message.id), message)
                self.assertEqual(self.sql[self.post_commit_start:], [])
            finally:
                self.hold_after_commit = False; self.release_lock()
            actual = self.rows()
            self.assertTrue(any(r['id'] == 'pending' for r in actual['novel']))
            self.assertEqual(next(r for r in actual['novel'] if r['id'] == 'foreign')['title'], '共同改名🐾')
            self.assertEqual(next(r for r in actual['message'] if r['id'] == message.id)['content'], message.content)

    def test_flush_failure_and_missing_guard_preserve_original_caller_contract(self):
        with self.sessions() as db:
            pending = models.Novel(id='pending', title='调用方pending')
            db.add(pending)
            with self.assertRaises(HTTPException) as error:
                repos.ConversationRepo(db).add_message('missing', 'user', '不应保存')
            self.assertEqual(error.exception.status_code, 404)
            self.assertIn(pending, db.new)
            db.rollback()
        with self.engine.begin() as db:
            db.exec_driver_sql("CREATE TRIGGER reject_message BEFORE INSERT ON message BEGIN SELECT RAISE(ABORT,'controlled message flush rejection'); END")
        try:
            with self.sessions() as db:
                db.add(models.Novel(id='pending', title='调用方pending'))
                with self.assertRaises(Exception):
                    repos.ConversationRepo(db).add_message('global', 'assistant', '拒写原稿😀')
                self.assertFalse(db.is_active)
                self.assertEqual(self.rows(), self.before)
                db.rollback()
                self.assertTrue(db.is_active)
        finally:
            with self.engine.begin() as db:
                db.exec_driver_sql('DROP TRIGGER reject_message')


if __name__ == '__main__':
    unittest.main()
