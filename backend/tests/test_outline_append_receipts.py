"""大纲追加未知回应的重放必须返回原树，不能二次追加或复活删除节点。"""
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import update

from app.application import outline_append, outline_plan
from app.infrastructure import models, repository as repos
from app.interfaces.schemas import OutlineVolumeDraft
import test_other_delete_transactions as fixtures


class OutlineAppendReceiptTest(unittest.TestCase):
    rows = fixtures.OtherDeleteTransactionTest.rows
    release_lock = fixtures.OtherDeleteTransactionTest.release_lock
    assert_writer_released = fixtures.OtherDeleteTransactionTest.assert_writer_released

    def setUp(self):
        fixtures.OtherDeleteTransactionTest.setUp(self)
        self.request_id = str(uuid4())
        self.plan = {'title': '  一卷😀  ', 'summary': '\n　卷概要🐾\0\n',
            'chapters': [{'title': '  直属章  ', 'summary': '直属概要'}],
            'acts': [{'title': '  第一幕  ', 'summary': '幕概要', 'chapters': [{'title': '幕内章', 'summary': '幕内概要'}]}]}
        self.url = '/api/v1/novels/work/outline/apply'

    def append(self, **overrides):
        return self.client.post(self.url, json={'plan': self.plan, 'request_id': self.request_id, **overrides})

    def test_native_same_request_replay_returns_original_full_subtree_without_new_commit(self):
        first = self.append(); self.assertEqual(first.status_code, 200)
        fact = first.json(); self.assertEqual(len(fact), 4)
        self.assertEqual(fact['request_id'], self.request_id); self.assertFalse(fact['append_replayed'])
        self.assertEqual(len(fact['node_ids']), 4); self.assertEqual(fact['node_ids'][0], fact['volume_id'])
        before = self.rows(); commits = self.commit_count
        second = self.append(); self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), {**fact, 'append_replayed': True})
        self.assertEqual(self.commit_count, commits); self.assertEqual(self.rows(), before)
        nodes = {r['id']: r for r in before['outline_node']}
        volume, direct, act, nested = [nodes[id] for id in fact['node_ids']]
        self.assertEqual(volume['title'], '一卷😀'); self.assertEqual(volume['summary'], self.plan['summary'])
        self.assertEqual(direct['parent_id'], volume['id']); self.assertEqual(act['parent_id'], volume['id'])
        self.assertEqual(nested['parent_id'], act['id']); self.assertEqual([direct['sort_order'], act['sort_order'], nested['sort_order']], [1, 2, 1])

    def test_same_UUID_changed_raw_payload_rejected_without_mutating_original_tree(self):
        self.assertEqual(self.append().status_code, 200); before = self.rows()
        altered = {**self.plan, 'title': '一卷😀'}  # 即使存储trim后同名，原请求原文也不能替换。
        result = self.append(plan=altered)
        self.assertEqual(result.status_code, 409); self.assertEqual(result.json()['code'], 'outline_append_payload_conflict')
        self.assertEqual(self.rows(), before)

    def test_same_UUID_other_scope_rejected(self):
        self.append(); before = self.rows()
        result = self.client.post('/api/v1/novels/foreign/outline/apply', json={'plan': self.plan, 'request_id': self.request_id})
        self.assertEqual(result.status_code, 409); self.assertEqual(result.json()['code'], 'outline_append_scope_conflict')
        self.assertEqual(self.rows(), before)

    def test_deleted_leaf_replay_never_recreates_or_patches_half_tree(self):
        fact = self.append().json()
        self.assertEqual(self.client.delete('/api/v1/novels/work/outline/' + fact['node_ids'][1]).status_code, 204)
        before = self.rows(); response = self.append()
        self.assertEqual(response.status_code, 409); self.assertEqual(response.json()['code'], 'outline_append_deleted')
        self.assertEqual(self.rows(), before); self.assertEqual(len(before['outline_append_receipt']), 1)

    def test_current_author_edits_remain_unchanged_on_receipt_replay(self):
        fact = self.append().json()
        self.assertEqual(self.client.put('/api/v1/novels/work/outline/' + fact['volume_id'], json={'title': '作者后来改标题', 'summary': '后来概要'}).status_code, 200)
        before = self.rows(); response = self.append()
        self.assertTrue(response.json()['append_replayed']); self.assertEqual(self.rows(), before)

    def test_COMMIT_denial_rolls_back_nodes_and_receipt_before_next_native_POST(self):
        self.armed = True; response = self.append()
        self.assertEqual(response.status_code, 500); self.assertEqual(self.denials, ['COMMIT'])
        self.assertEqual(self.closed[-1], (False, False)); self.assertEqual(self.rows(), self.before)
        self.assert_writer_released()
        created = self.client.post('/api/v1/novels', json={'title': '下一合法作品'})
        self.assertEqual(created.status_code, 201)
        after = self.rows(); after['novel'] = [r for r in after['novel'] if r['id'] != created.json()['id']]
        self.assertEqual(after, self.before)

    def test_receipt_flush_failure_rolls_back_complete_subtree(self):
        with self.engine.begin() as db:
            db.exec_driver_sql("CREATE TRIGGER reject_receipt BEFORE INSERT ON outline_append_receipt BEGIN SELECT RAISE(ABORT,'receipt rejected'); END")
        self.assertEqual(self.append().status_code, 500); self.assertEqual(self.rows(), self.before)
        self.assertEqual(self.closed[-1], (False, False)); self.assert_writer_released()

    def test_native_response_under_real_postcommit_exclusive_lock_uses_no_SQL(self):
        self.hold_after_commit = True; commits = self.commit_count
        response = self.append(); self.assertEqual(response.status_code, 200)
        self.assertEqual(self.commit_count - commits, 1); self.assertEqual(len(response.json()['node_ids']), 4)
        self.assertEqual(self.sql[self.post_commit_start:], [])
        self.assertEqual(self.closed[-1], (False, False)); self.release_lock()

    def test_database_reconnect_replays_durable_receipt(self):
        first = self.append().json(); before = self.rows(); self.engine.dispose()
        second = self.append(); self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), {**first, 'append_replayed': True}); self.assertEqual(self.rows(), before)

    def test_two_concurrent_initial_reads_same_UUID_commit_exactly_one_tree(self):
        barrier = threading.Barrier(2); local = threading.local(); original = outline_append._receipt
        def synchronize(db, request_id):
            result = original(db, request_id)
            if not getattr(local, 'seen', False):
                local.seen = True; self.assertIsNone(result); barrier.wait(timeout=5)
            return result
        with patch.object(outline_append, '_receipt', side_effect=synchronize), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.append(), range(2)))
        self.assertEqual([r.status_code for r in results], [200, 200])
        facts = [r.json() for r in results]
        self.assertEqual(facts[0]['node_ids'], facts[1]['node_ids'])
        self.assertEqual(sorted(r['append_replayed'] for r in facts), [False, True])
        self.assertEqual(len(self.rows()['outline_append_receipt']), 1)
        self.assertEqual(len(self.rows()['outline_node']), len(self.before['outline_node']) + 4)

    def test_dirty_or_Core_caller_transactions_rejected_without_rollback(self):
        for mode in ('pending', 'Core'):
            with self.subTest(mode=mode), self.sessions() as db:
                if mode == 'pending': db.add(models.Novel(id='pending', title='调用方未提交'))
                else: db.execute(update(models.Novel).where(models.Novel.id == 'foreign').values(title='调用方Core'))
                with self.assertRaises(RuntimeError):
                    outline_append.append_outline_once(db, 'work', OutlineVolumeDraft.model_validate(self.plan), self.request_id)
                self.assertTrue(db.in_transaction())
                if mode == 'pending': self.assertEqual(len(db.new), 1)
                else: self.assertTrue(db.connection().connection.driver_connection.in_transaction)
                db.commit()
        after = self.rows()
        self.assertTrue(any(r['id'] == 'pending' for r in after['novel']))
        self.assertEqual(next(r for r in after['novel'] if r['id'] == 'foreign')['title'], '调用方Core')
        self.assertEqual(after['outline_append_receipt'], [])

    def test_legacy_no_UUID_keeps_deliberate_append_and_shared_pending_commit(self):
        with self.sessions() as db:
            db.add(models.Novel(id='pending', title='旧调用方共同提交'))
            first = outline_plan.apply_outline(db, 'work', OutlineVolumeDraft.model_validate(self.plan))
            second = outline_plan.apply_outline(db, 'work', OutlineVolumeDraft.model_validate(self.plan))
            self.assertNotEqual(first['volume_id'], second['volume_id'])
        after = self.rows(); self.assertEqual(after['outline_append_receipt'], [])
        self.assertTrue(any(r['id'] == 'pending' for r in after['novel']))
        self.assertEqual(len(after['outline_node']), len(self.before['outline_node']) + 8)

    def test_invalid_UUID_plan_and_missing_novel_never_write(self):
        self.assertEqual(self.append(request_id='invalid').status_code, 422)
        self.assertEqual(self.append(plan={'title': '空卷', 'chapters': []}).status_code, 422)
        response = self.client.post('/api/v1/novels/missing/outline/apply', json={'plan': self.plan, 'request_id': self.request_id})
        self.assertEqual(response.status_code, 404); self.assertEqual(self.rows(), self.before)

    def test_distinct_UUID_same_plan_is_explicit_new_volume(self):
        first = self.append().json(); second = self.append(request_id=str(uuid4())).json()
        self.assertNotEqual(first['volume_id'], second['volume_id']); self.assertFalse(second['append_replayed'])
        self.assertEqual(len(self.rows()['outline_append_receipt']), 2)

    def test_novel_delete_cleans_its_receipt_and_keeps_foreign_global_data(self):
        self.append(); response = self.client.delete('/api/v1/novels/work')
        self.assertEqual(response.status_code, 204); after = self.rows()
        self.assertEqual(after['outline_append_receipt'], [])
        self.assertEqual(after['novel'], [r for r in self.before['novel'] if r['id'] == 'foreign'])
        self.assertEqual(after['message'], [r for r in self.before['message'] if r['conversation_id'] == 'global'])
