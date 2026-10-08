"""消息meta合并失败不能被下一请求提交，正文与作用域保持。"""
import unittest
from fastapi import HTTPException
from sqlalchemy import event, update

from app.infrastructure import models, repository as repos
import test_other_delete_transactions as fixtures


class MessageMetaFailedTransactionTest(unittest.TestCase):
    rows = fixtures.OtherDeleteTransactionTest.rows
    release_lock = fixtures.OtherDeleteTransactionTest.release_lock
    assert_writer_released = fixtures.OtherDeleteTransactionTest.assert_writer_released

    def setUp(self):
        fixtures.OtherDeleteTransactionTest.setUp(self)
        with self.sessions() as db:
            db.get(models.Message, 'message').meta = {'old': '精确原meta😀', 'nested': {'old': 1}}
            db.commit()
        self.before = self.rows()
        self.url = '/api/v1/conversations/conversation/messages/message'
        self.patch = {'applied': True, 'nested': {'new': '新值🐾\0'}}

    def test_native_COMMIT_denial_no_meta_leak_on_next_POST(self):
        self.armed = True
        response = self.client.patch(self.url, json={'meta': self.patch})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.denials, ['COMMIT'])
        self.assertEqual(self.closed[-1], (False, False))
        self.assertEqual(self.rows(), self.before)
        self.assert_writer_released()
        created = self.client.post('/api/v1/novels', json={'title': '下一合法作品😀'})
        self.assertEqual(created.status_code, 201)
        actual = self.rows()
        actual['novel'] = [r for r in actual['novel'] if r['id'] != created.json()['id']]
        self.assertEqual(actual, self.before)

    def test_direct_COMMIT_denial_rolls_back_callers_pending_and_Core(self):
        with self.sessions() as db:
            db.add(models.Novel(id='pending', title='调用方pending'))
            db.execute(update(models.Chapter).where(models.Chapter.id == 'foreign-chapter')
                .values(directive='调用方Core未提交'))
            driver = db.connection().connection.driver_connection
            driver.set_authorizer(self.authorizer)
            self.armed = True
            try:
                with self.assertRaises(Exception):
                    repos.ConversationRepo(db).update_message('conversation', 'message', meta=self.patch)
                self.assertFalse(driver.in_transaction)
                self.assertFalse(db.in_transaction())
            finally:
                driver.set_authorizer(None)
        self.assertEqual(self.rows(), self.before)
        self.assert_writer_released()

    def test_real_flush_failure_preserves_inactive_caller_contract_and_native_cleanup(self):
        with self.engine.begin() as db:
            db.exec_driver_sql("CREATE TRIGGER reject_meta BEFORE UPDATE ON message BEGIN SELECT RAISE(ABORT,'controlled meta flush rejection'); END")
        try:
            with self.sessions() as db:
                db.add(models.Novel(id='pending', title='调用方pending'))
                with self.assertRaises(Exception):
                    repos.ConversationRepo(db).update_message('conversation', 'message', meta=self.patch)
                self.assertFalse(db.is_active)
                self.assertEqual(self.rows(), self.before)
                db.rollback()
                self.assertTrue(db.is_active)
            self.assertEqual(self.client.patch(self.url, json={'meta': self.patch}).status_code, 500)
            self.assertEqual(self.closed[-1], (False, False))
            self.assertEqual(self.rows(), self.before)
            self.assert_writer_released()
        finally:
            with self.engine.begin() as db:
                db.exec_driver_sql('DROP TRIGGER reject_meta')

    def test_native_success_shallow_merge_full6_keeps_role_content_and_other20_rows(self):
        commits = self.commit_count
        response = self.client.patch(self.url, json={'meta': self.patch})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.commit_count - commits, 1)
        fact = response.json()
        self.assertEqual(len(fact), 6)
        old = next(r for r in self.before['message'] if r['id'] == 'message')
        for key in ('id', 'conversation_id', 'role', 'content', 'created_at'):
            self.assertEqual(fact[key], old[key])
        self.assertEqual(fact['meta'], {'old': '精确原meta😀', **self.patch})
        actual = self.rows()
        actual_message = next(r for r in actual['message'] if r['id'] == 'message')
        import json
        self.assertEqual(json.loads(actual_message['meta']), fact['meta'])
        actual_message['meta'] = old['meta']
        self.assertEqual(actual, self.before)
        self.assertEqual(self.client.patch(self.url, json={'meta': None}).json()['meta'], fact['meta'])
        self.assertEqual(self.client.patch(self.url, json={'meta': {}}).json()['meta'], fact['meta'])

    def test_success_same_ORM_commits_callers_new_dirty_Core_with_legal_refresh(self):
        starts = []
        def committed(db):
            starts.append(len(self.sql))
        event.listen(self.sessions.class_, 'after_commit', committed)
        self.addCleanup(event.remove, self.sessions.class_, 'after_commit', committed)
        with self.sessions() as db:
            db.add(models.Novel(id='pending', title='共同创建😀'))
            db.get(models.Novel, 'foreign').title = '共同改名🐾'
            db.execute(update(models.Chapter).where(models.Chapter.id == 'foreign-chapter')
                .values(directive='共同Core指令'))
            original = db.get(models.Message, 'message')
            commits = self.commit_count
            returned = repos.ConversationRepo(db).update_message('conversation', 'message', meta=self.patch)
            self.assertIs(returned, original)
            self.assertEqual(self.commit_count - commits, 1)
            self.assertEqual(len(starts), 1)
            postcommit = self.sql[starts[0]:]
            self.assertTrue(postcommit)
            self.assertTrue(all(s.lstrip().startswith('SELECT') for s in postcommit))
            actual = self.rows()
            self.assertTrue(any(r['id'] == 'pending' for r in actual['novel']))
            self.assertEqual(next(r for r in actual['novel'] if r['id'] == 'foreign')['title'], '共同改名🐾')
            self.assertEqual(next(r for r in actual['chapter'] if r['id'] == 'foreign-chapter')['directive'], '共同Core指令')

    def test_missing_and_foreign_scope_reject_without_clearing_callers_pending(self):
        with self.sessions() as db:
            pending = models.Novel(id='pending', title='未提交')
            db.add(pending)
            for cid, mid in (('conversation', 'missing'), ('global', 'message'), ('missing', 'message')):
                with self.assertRaises(HTTPException) as error:
                    repos.ConversationRepo(db).update_message(cid, mid, meta=self.patch)
                self.assertEqual(error.exception.status_code, 404)
                self.assertIn(pending, db.new)
            db.rollback()
        self.assertEqual(self.client.patch('/api/v1/conversations/global/messages/message', json={'meta': self.patch}).status_code, 404)
        self.assertEqual(self.rows(), self.before)


if __name__ == '__main__':
    unittest.main()
