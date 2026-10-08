"""大纲创建/更新/排序失败，不得由下一请求提交原失败写入。"""
import unittest
from fastapi import HTTPException
from sqlalchemy import update

import test_other_delete_transactions as fixtures
from app.infrastructure import models, repository as repos


class OutlineFailedTransactionTest(unittest.TestCase):
    rows = fixtures.OtherDeleteTransactionTest.rows
    release_lock = fixtures.OtherDeleteTransactionTest.release_lock
    assert_writer_released = fixtures.OtherDeleteTransactionTest.assert_writer_released

    def setUp(self):
        fixtures.OtherDeleteTransactionTest.setUp(self)
        with self.sessions() as db:
            db.add_all([models.OutlineNode(id='node2', novel_id='work', level='volume', title='同级另一节点'),
                models.OutlineNode(id='child', novel_id='work', parent_id='node', level='chapter', title='不同级子章'),
                models.OutlineNode(id='foreign-node', novel_id='foreign', level='volume', title='外作品节点')])
            db.commit()
        self.before = self.rows()
        self.cases = [
            ('outline_create', 'POST', '/api/v1/novels/work/outline', {'title': '明确新建大纲😀', 'level': 'volume'},
                lambda db: repos.OutlineRepo(db).create('work', title='明确新建大纲😀', level='volume'), 'outline_node', 'INSERT'),
            ('outline_update', 'PUT', '/api/v1/novels/work/outline/node', {'title': '明确修改大纲😀'},
                lambda db: repos.OutlineRepo(db).update('work', 'node', title='明确修改大纲😀'), 'outline_node', 'UPDATE'),
            ('outline_reorder', 'POST', '/api/v1/novels/work/outline/reorder', {'ordered_ids': ['node2', 'node']},
                lambda db: repos.OutlineRepo(db).reorder('work', ['node2', 'node']), 'outline_node', 'UPDATE')]

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

    def test_direct_flush_failure_preserves_original_inactive_Session_for_caller_rollback(self):
        for kind, method, path, payload, operation, table, command in self.cases:
            with self.subTest(kind=kind):
                with self.engine.begin() as db:
                    db.exec_driver_sql(f"CREATE TRIGGER reject_outline BEFORE {command} ON outline_node BEGIN SELECT RAISE(ABORT,'controlled outline flush rejection'); END")
                try:
                    with self.sessions() as db:
                        db.add(models.Novel(id='pending', title='原调用方未提交'))
                        db.execute(update(models.Chapter).where(models.Chapter.id == 'foreign-chapter')
                            .values(directive='调用方Core未提交'))
                        driver = db.connection().connection.driver_connection
                        with self.assertRaises(Exception):
                            operation(db)
                        self.assertFalse(db.is_active)
                        self.assertFalse(driver.in_transaction)
                        self.assertEqual(self.rows(), self.before)
                        db.rollback()
                        self.assertTrue(db.is_active)
                    self.assert_writer_released()
                finally:
                    with self.engine.begin() as db:
                        db.exec_driver_sql('DROP TRIGGER reject_outline')

    def test_native_success_preserves_complete9_and_legal_postcommit_read(self):
        for kind, method, path, payload, operation, table, command in self.cases:
            with self.subTest(kind=kind):
                commits = self.commit_count
                response = self.client.request(method, path, json=payload)
                self.assertEqual(response.status_code, 201 if kind == 'outline_create' else 200)
                self.assertEqual(self.commit_count - commits, 1)
                facts = response.json() if kind == 'outline_reorder' else [response.json()]
                for fact in facts:
                    self.assertEqual(len(fact), 9)
                    self.assertEqual(fact['novel_id'], 'work')
                    self.assertEqual(next(r for r in self.rows()[table] if r['id'] == fact['id']), fact)
                if kind == 'outline_reorder':
                    indexed = {r['id']: r['sort_order'] for r in facts}
                    self.assertEqual((indexed['node2'], indexed['node']), (0, 1))
                else:
                    for key, value in payload.items():
                        self.assertEqual(facts[0][key], value)
                for table_name in self.before:
                    if table_name != 'outline_node':
                        self.assertEqual(self.rows()[table_name], self.before[table_name])

    def test_success_preserves_same_ORM_identity_and_shared_pending_Core_commit(self):
        for kind, method, path, payload, operation, table, command in self.cases:
            with self.subTest(kind=kind), self.sessions() as db:
                pending = models.Novel(id=f'pending-{kind}', title='调用方共同新建😀')
                db.add(pending)
                db.get(models.Novel, 'foreign').title = f'调用方共同改名-{kind}'
                db.execute(update(models.Chapter).where(models.Chapter.id == 'foreign-chapter')
                    .values(directive=f'调用方Core指令-{kind}'))
                expected = None
                if kind == 'outline_update':
                    model, identifier = models.OutlineNode, 'node'
                    expected = db.get(model, identifier)
                commits = self.commit_count
                returned = operation(db)
                self.assertEqual(self.commit_count - commits, 1)
                if expected is not None:
                    self.assertIs(returned, expected)
                if returned is not None:
                    self.assertIs(db.get(type(returned), returned.id), returned)
                self.assertTrue(any(r['id'] == pending.id for r in self.rows()['novel']))
                self.assertEqual(next(r for r in self.rows()['novel'] if r['id'] == 'foreign')['title'], f'调用方共同改名-{kind}')
                self.assertEqual(next(r for r in self.rows()['chapter'] if r['id'] == 'foreign-chapter')['directive'], f'调用方Core指令-{kind}')

    def test_missing_scope_and_reorder_guards_preserve_callers_pending(self):
        with self.sessions() as db:
            pending = models.Novel(id='pending', title='原调用方pending')
            db.add(pending)
            for operation in (lambda: repos.OutlineRepo(db).update('work', 'missing', title='不应写'),
                lambda: repos.OutlineRepo(db).create('missing', title='不应创建', level='volume'),
                lambda: repos.OutlineRepo(db).reorder('work', ['missing'])):
                with self.assertRaises(HTTPException) as error:
                    operation()
                self.assertEqual(error.exception.status_code, 404)
                self.assertIn(pending, db.new)
            db.rollback()
        self.assertEqual(self.rows(), self.before)
        for payload in ({'ordered_ids':['node','foreign-node']}, {'ordered_ids':['missing']}):
            response = self.client.post('/api/v1/novels/work/outline/reorder',json=payload)
            self.assertEqual(response.status_code,404)
        self.assertEqual(self.rows(),self.before)
        response = self.client.post('/api/v1/novels/work/outline/reorder',json={'ordered_ids':['node','child']})
        self.assertEqual(response.status_code,400)
        self.assertEqual(self.rows(),self.before)



if __name__ == '__main__':
    unittest.main()
