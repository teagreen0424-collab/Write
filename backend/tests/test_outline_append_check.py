"""原追加核对只能读取回执；旧服务404不能等同可安全追加。"""
import unittest
from uuid import uuid4
from sqlalchemy import update
from app.application.outline_append import check_outline_append, OutlineAppendError
from app.infrastructure import models
from app.interfaces.schemas import OutlineVolumeDraft
import test_other_delete_transactions as fixtures


class OutlineAppendCheckTest(unittest.TestCase):
    rows = fixtures.OtherDeleteTransactionTest.rows
    release_lock = fixtures.OtherDeleteTransactionTest.release_lock

    def setUp(self):
        fixtures.OtherDeleteTransactionTest.setUp(self)
        self.request_id = str(uuid4())
        self.plan = {'title':'原卷😀', 'summary':'\n　原稿🐾\0', 'chapters':[{'title':'章','summary':'章稿'}], 'acts':[]}
        self.url = f'/api/v1/novels/work/outline/append-requests/{self.request_id}/check'

    def append(self):
        response = self.client.post('/api/v1/novels/work/outline/apply', json={'request_id':self.request_id,'plan':self.plan})
        self.assertEqual(response.status_code,200)
        return response.json()

    def check(self, plan=None, novel='work'):
        return self.client.post(self.url.replace('/work/',f'/{novel}/'), json={'plan':plan or self.plan})

    def assert_read_only(self, before, commits, start):
        self.assertEqual(self.rows(),before); self.assertEqual(self.commit_count,commits)
        self.assertFalse(any(s.lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for s in self.sql[start:]))

    def test_missing_native_typed_404_has_zero_writes(self):
        commits,start=self.commit_count,len(self.sql)
        response=self.check(); self.assertEqual(response.status_code,404)
        self.assertEqual(response.json()['code'],'outline_append_not_found')
        self.assert_read_only(self.before,commits,start)

    def test_saved_native_full_receipt_read_without_second_commit(self):
        fact=self.append(); before=self.rows(); commits,start=self.commit_count,len(self.sql)
        response=self.check(); self.assertEqual(response.status_code,200)
        self.assertEqual(response.json(),{**fact,'append_replayed':True})
        self.assert_read_only(before,commits,start)

    def test_scope_and_raw_payload_conflict_preserve_all_tables(self):
        self.append(); before=self.rows(); commits,start=self.commit_count,len(self.sql)
        for novel,plan,code in [('foreign',self.plan,'outline_append_scope_conflict'),('work',{**self.plan,'summary':'改稿'},'outline_append_payload_conflict')]:
            response=self.check(plan,novel); self.assertEqual(response.status_code,409); self.assertEqual(response.json()['code'],code)
        self.assert_read_only(before,commits,start)

    def test_deleted_leaf_check_does_not_resurrect(self):
        fact=self.append(); self.client.delete('/api/v1/novels/work/outline/'+fact['node_ids'][1])
        before=self.rows(); commits,start=self.commit_count,len(self.sql)
        response=self.check(); self.assertEqual(response.status_code,409); self.assertEqual(response.json()['code'],'outline_append_deleted')
        self.assert_read_only(before,commits,start)

    def test_later_author_edits_remain_on_original_receipt_check(self):
        fact=self.append(); self.client.put('/api/v1/novels/work/outline/'+fact['volume_id'],json={'title':'作者后来标题','summary':'后来稿'})
        before=self.rows(); commits,start=self.commit_count,len(self.sql)
        self.assertEqual(self.check().json(),{**fact,'append_replayed':True})
        self.assert_read_only(before,commits,start)

    def test_readonly_caller_pending_and_Core_write_neither_flush_commit_nor_rollback(self):
        fact=self.append()
        with self.sessions() as db:
            pending=models.Novel(id='pending',title='调用方未提交'); db.add(pending)
            db.execute(update(models.Novel).where(models.Novel.id=='foreign').values(title='Core未提交'))
            commits,start=self.commit_count,len(self.sql)
            self.assertEqual(check_outline_append(db,'work',OutlineVolumeDraft(**self.plan),self.request_id),{**fact,'append_replayed':True})
            with self.assertRaises(OutlineAppendError): check_outline_append(db,'work',OutlineVolumeDraft(**self.plan),str(uuid4()))
            self.assertIn(pending,db.new); self.assertTrue(db.in_transaction()); self.assertEqual(self.commit_count,commits)
            self.assertFalse(any(s.lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for s in self.sql[start:]))
            self.assertEqual(db.execute(models.Novel.__table__.select().where(models.Novel.id=='foreign')).mappings().one()['title'],'Core未提交')
            db.rollback()

    def test_invalid_UUID_and_missing_novel_do_not_append(self):
        commits,start=self.commit_count,len(self.sql)
        self.assertEqual(self.client.post(self.url.replace(self.request_id,'not-uuid'),json={'plan':self.plan}).status_code,422)
        self.assertEqual(self.check(novel='missing').status_code,404)
        self.assert_read_only(self.before,commits,start)
