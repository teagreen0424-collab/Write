"""元资料创建/更新失败，不得由下一请求提交原失败写入。"""
import unittest
from fastapi import HTTPException
from sqlalchemy import update

import test_other_delete_transactions as fixtures
from app.infrastructure import models, repository as repos


class MetadataFailedTransactionTest(unittest.TestCase):
    rows = fixtures.OtherDeleteTransactionTest.rows
    release_lock = fixtures.OtherDeleteTransactionTest.release_lock
    assert_writer_released = fixtures.OtherDeleteTransactionTest.assert_writer_released

    def setUp(self):
        fixtures.OtherDeleteTransactionTest.setUp(self)
        self.cases = [
            ('novel_create', 'POST', '/api/v1/novels', {'title': '明确创建作品😀'},
                lambda db: repos.NovelRepo(db).create(title='明确创建作品😀'), 'novel', 'INSERT'),
            ('novel_update', 'PUT', '/api/v1/novels/work', {'title': '明确改名作品😀'},
                lambda db: repos.NovelRepo(db).update('work', title='明确改名作品😀'), 'novel', 'UPDATE'),
            ('conversation_create', 'POST', '/api/v1/conversations', {'novel_id': 'work'},
                lambda db: repos.ConversationRepo(db).create(novel_id='work'), 'conversation', 'INSERT'),
            ('conversation_update', 'PATCH', '/api/v1/conversations/conversation', {'title': '明确改名会话😀'},
                lambda db: repos.ConversationRepo(db).update('conversation', title='明确改名会话😀'), 'conversation', 'UPDATE')]

    def test_COMMIT_denial_releases_driver_and_next_native_POST_cannot_commit_failed_write(self):
        for kind, method, path, payload, operation, table, command in self.cases:
            with self.subTest(kind=kind):
                self.armed = True
                response = self.client.request(method, path, json=payload)
                self.assertEqual(response.status_code, 500)
                self.assertEqual(self.closed[-1], (False, False))
                self.assert_writer_released()
                self.assertEqual(self.rows(), self.before)
                created = self.client.post('/api/v1/novels', json={'title': '下一合法创建🐾'})
                self.assertEqual(created.status_code, 201)
                actual = self.rows()
                actual['novel'] = [r for r in actual['novel'] if r['id'] != created.json()['id']]
                self.assertEqual(actual, self.before)
                with self.sessions() as db:
                    db.query(models.Novel).filter(models.Novel.id == created.json()['id']).delete(); db.commit()

    def test_direct_caller_COMMIT_denial_rolls_back_entire_shared_write(self):
        for kind, method, path, payload, operation, table, command in self.cases:
            with self.subTest(kind=kind), self.sessions() as db:
                db.add(models.Novel(id='pending', title='调用方未提交😀'))
                db.get(models.Novel, 'foreign').title = '调用方未提交修改'
                driver = db.connection().connection.driver_connection
                driver.set_authorizer(self.authorizer)
                self.armed = True
                try:
                    with self.assertRaises(Exception):
                        operation(db)
                    self.assertFalse(driver.in_transaction)
                    self.assertFalse(db.in_transaction())
                finally:
                    driver.set_authorizer(None)
                self.assertEqual(self.rows(), self.before)
                self.assert_writer_released()

    def test_real_flush_abort_preserves_all20_original_rows(self):
        for kind, method, path, payload, operation, table, command in self.cases:
            with self.subTest(kind=kind):
                with self.engine.begin() as db:
                    db.exec_driver_sql(f"CREATE TRIGGER reject_metadata BEFORE {command} ON {table} BEGIN SELECT RAISE(ABORT,'controlled metadata flush rejection'); END")
                try:
                    self.assertEqual(self.client.request(method, path, json=payload).status_code, 500)
                    self.assertEqual(self.closed[-1], (False, False))
                    self.assertEqual(self.rows(), self.before)
                    self.assert_writer_released()
                finally:
                    with self.engine.begin() as db:
                        db.exec_driver_sql('DROP TRIGGER reject_metadata')

    def test_native_success_complete_facts_and_no_postcommit_SQL_under_exclusive_lock(self):
        for kind, method, path, payload, operation, table, command in self.cases:
            with self.subTest(kind=kind):
                self.hold_after_commit = True
                commits = self.commit_count
                response = self.client.request(method, path, json=payload)
                try:
                    self.assertEqual(response.status_code, 201 if method == 'POST' else 200)
                    fact = response.json()
                    self.assertEqual(len(fact), 7 if table == 'novel' else 5)
                    for key, value in payload.items():
                        self.assertEqual(fact[key], value)
                    self.assertEqual(self.commit_count - commits, 1)
                    self.assertIsNotNone(self.lock)
                    self.assertEqual(self.sql[self.post_commit_start:], [])
                finally:
                    self.hold_after_commit = False; self.release_lock()
                physical = next(r for r in self.rows()[table] if r['id'] == fact['id'])
                self.assertEqual(physical, fact)

    def test_success_preserves_same_ORM_identity_and_shared_pending_Core_commit(self):
        for kind, method, path, payload, operation, table, command in self.cases:
            with self.subTest(kind=kind), self.sessions() as db:
                pending = models.Novel(id=f'pending-{kind}', title='调用方共同新建😀')
                db.add(pending)
                db.get(models.Novel, 'foreign').title = f'调用方共同改名-{kind}'
                db.execute(update(models.Chapter).where(models.Chapter.id == 'foreign-chapter')
                    .values(directive=f'调用方Core指令-{kind}'))
                expected = None
                if kind.endswith('_update'):
                    model, identifier = (models.Novel, 'work') if table == 'novel' else (models.Conversation, 'conversation')
                    expected = db.get(model, identifier)
                commits = self.commit_count
                returned = operation(db)
                self.assertEqual(self.commit_count - commits, 1)
                if expected is not None:
                    self.assertIs(returned, expected)
                self.assertIs(db.get(type(returned), returned.id), returned)
                self.assertTrue(any(r['id'] == pending.id for r in self.rows()['novel']))
                self.assertEqual(next(r for r in self.rows()['novel'] if r['id'] == 'foreign')['title'], f'调用方共同改名-{kind}')
                self.assertEqual(next(r for r in self.rows()['chapter'] if r['id'] == 'foreign-chapter')['directive'], f'调用方Core指令-{kind}')

    def test_missing_update_preserves_callers_pending_transaction(self):
        with self.sessions() as db:
            pending = models.Novel(id='pending', title='原调用方pending')
            db.add(pending)
            for operation in (lambda: repos.NovelRepo(db).update('missing', title='不应写'),
                lambda: repos.ConversationRepo(db).update('missing', title='不应写')):
                with self.assertRaises(HTTPException) as error:
                    operation()
                self.assertEqual(error.exception.status_code, 404)
                self.assertIn(pending, db.new)
            for operation, status in (
                (lambda: repos.ConversationRepo(db).update('conversation', novel_id='foreign'), 400),
                (lambda: repos.ConversationRepo(db).create(novel_id='missing'), 404)):
                with self.assertRaises(HTTPException) as error:
                    operation()
                self.assertEqual(error.exception.status_code, status)
                self.assertIn(pending, db.new)
                self.assertTrue(db.in_transaction())
            db.add(models.Conversation(id='unbound', novel_id=None))
            db.flush()
            with self.assertRaises(HTTPException) as error:
                repos.ConversationRepo(db).update('unbound', novel_id='missing')
            self.assertEqual(error.exception.status_code, 404)
            self.assertIsNotNone(db.get(models.Novel, 'pending'))
            self.assertTrue(db.in_transaction())
            db.rollback()
        self.assertEqual(self.rows(), self.before)


if __name__ == '__main__':
    unittest.main()
