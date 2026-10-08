"""作品、大纲和会话删除失败必须释放真实 SQLite 事务。"""
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.infrastructure import db as production, models, repository as repos
from app.interfaces.api.v1 import novels, outline, conversations


class OtherDeleteTransactionTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'delete.sqlite'
        self.engine = create_engine(f'sqlite:///{self.path.as_posix()}',
            connect_args={'check_same_thread': False, 'timeout': .2})
        self.addCleanup(self.engine.dispose)
        models.Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        self.armed = False
        self.denials, self.closed, self.sql = [], [], []
        self.lock = None
        self.hold_after_commit = False
        self.commit_count = 0
        self.post_commit_start = None
        self.addCleanup(self.release_lock)
        for target in ('app.infrastructure.ai.llm_provider.LLMProvider.complete',
            'app.infrastructure.ai.llm_provider.LLMProvider.stream_complete',
            'app.infrastructure.ai.embedding.EmbeddingService.embed'):
            guard = patch(target, side_effect=AssertionError('DELETE must not call a provider'))
            guard.start(); self.addCleanup(guard.stop)
        session_patch = patch.object(production, 'SessionLocal', self.sessions)
        session_patch.start(); self.addCleanup(session_patch.stop)
        def authorizer(action, first, *args):
            if action == sqlite3.SQLITE_TRANSACTION and first == 'COMMIT' and self.armed:
                self.armed = False
                self.denials.append(first)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self.authorizer = authorizer
        event.listen(self.engine, 'before_cursor_execute',
            lambda connection, cursor, statement, *args: self.sql.append(statement))
        def committed(db):
            self.commit_count += 1
            if self.hold_after_commit:
                self.lock = sqlite3.connect(self.path, timeout=.2, check_same_thread=False)
                self.lock.execute('BEGIN EXCLUSIVE')
                self.post_commit_start = len(self.sql)
        event.listen(self.sessions.class_, 'after_commit', committed)
        self.addCleanup(event.remove, self.sessions.class_, 'after_commit', committed)
        def dependency():
            generator = production.get_db()
            db = next(generator)
            driver = db.connection().connection.driver_connection
            driver.set_authorizer(authorizer)
            try:
                yield db
            finally:
                driver.set_authorizer(None)
                generator.close()
                self.closed.append((driver.in_transaction, db.in_transaction()))
        app = FastAPI()
        for router in (novels.router, outline.router, conversations.router):
            app.include_router(router, prefix='/api/v1')
        app.dependency_overrides[production.get_db] = dependency
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        with self.sessions() as db:
            db.add_all([models.Novel(id='work', title='原作品😀'), models.Novel(id='foreign', title='外作品🐾'),
                models.Character(id='character', novel_id='work', name='原人物'),
                models.OutlineNode(id='node', novel_id='work', level='volume', title='大纲'),
                models.Chapter(id='chapter', novel_id='work', seq=1, outline_node_id='node', content='原正文\0😀\r\n'),
                models.Chapter(id='foreign-chapter', novel_id='foreign', seq=1, content='外正文\0🐾'),
                models.Conversation(id='conversation', novel_id='work', title='目标会话'),
                models.Conversation(id='global', novel_id=None, title='全局保持'),
                models.Message(id='message', conversation_id='conversation', role='assistant', content='原消息\0😀'),
                models.Message(id='global-message', conversation_id='global', role='assistant', content='全局原文\0🐾'),
                models.ChatAction(id='action', message_id='message', novel_id='work', action_type='chapter', result_id='chapter'),
                models.ChapterRevision(id='revision', novel_id='work', chapter_id='chapter', revision_no=1, content='旧版本\0😀')])
            db.commit()
        self.before = self.rows()
        self.cases = [('novel', '/api/v1/novels/work', 'novel', lambda db: repos.NovelRepo(db).delete('work')),
            ('outline', '/api/v1/novels/work/outline/node', 'outline_node', lambda db: repos.OutlineRepo(db).delete('work', 'node')),
            ('conversation', '/api/v1/conversations/conversation', 'conversation', lambda db: repos.ConversationRepo(db).delete('conversation'))]

    def rows(self):
        with closing(sqlite3.connect(f'file:{self.path.as_posix()}?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            self.assertEqual(len(tables), 20)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            return {t: [dict(r) for r in db.execute(f'SELECT * FROM "{t}" ORDER BY rowid')] for t in tables}

    def release_lock(self):
        if self.lock is not None:
            self.lock.rollback(); self.lock.close(); self.lock = None

    def assert_writer_released(self):
        with closing(sqlite3.connect(self.path, timeout=.2)) as db:
            db.execute('BEGIN EXCLUSIVE'); db.rollback()

    def test_denied_commit_cannot_leak_into_next_native_POST(self):
        for kind, path, table, delete in self.cases:
            with self.subTest(kind=kind):
                self.armed = True
                self.assertEqual(self.client.delete(path).status_code, 500)
                self.assertEqual(self.closed[-1], (False, False))
                self.assert_writer_released()
                self.assertEqual(self.rows(), self.before)
                start = len(self.sql)
                created = self.client.post('/api/v1/novels', json={'title': '下一合法作品😀'})
                self.assertEqual(created.status_code, 201)
                self.assertFalse(any(s.lstrip().startswith('DELETE') for s in self.sql[start:]))
                actual = self.rows()
                actual['novel'] = [r for r in actual['novel'] if r['id'] != created.json()['id']]
                self.assertEqual(actual, self.before)
                # Remove only the test's extra novel via its own direct transaction.
                with self.sessions() as db:
                    db.query(models.Novel).filter(models.Novel.id == created.json()['id']).delete(); db.commit()

    def test_direct_caller_commit_denial_rolls_back_shared_pending(self):
        for kind, path, table, delete in self.cases:
            with self.subTest(kind=kind), self.sessions() as db:
                db.add(models.Novel(id='pending', title='同事务尚未提交'))
                db.get(models.Novel, 'foreign').title = '同事务修改'
                driver = db.connection().connection.driver_connection
                driver.set_authorizer(self.authorizer)
                self.armed = True
                try:
                    with self.assertRaises(Exception):
                        delete(db)
                    self.assertFalse(driver.in_transaction)
                    self.assertFalse(db.in_transaction())
                finally:
                    driver.set_authorizer(None)
                self.assertEqual(self.rows(), self.before)
                self.assert_writer_released()

    def test_final_DELETE_flush_failure_restores_all_preceding_cleanup(self):
        for kind, path, table, delete in self.cases:
            with self.subTest(kind=kind):
                with self.engine.begin() as db:
                    db.exec_driver_sql(f"CREATE TRIGGER reject_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT,'controlled final DELETE failure'); END")
                try:
                    self.assertEqual(self.client.delete(path).status_code, 500)
                    self.assertEqual(self.closed[-1], (False, False))
                    self.assertEqual(self.rows(), self.before)
                    self.assert_writer_released()
                finally:
                    with self.engine.begin() as db:
                        db.exec_driver_sql('DROP TRIGGER reject_delete')

    def test_native_success_cleanup_and_204_no_postcommit_SQL(self):
        # Ordered successful deletes exercise unlink, message/action cleanup, then whole-work cleanup.
        for kind in ('outline', 'conversation', 'novel'):
            case = next(r for r in self.cases if r[0] == kind)
            with self.subTest(kind=kind):
                before = self.rows(); commits = self.commit_count
                self.hold_after_commit = True
                response = self.client.delete(case[1])
                try:
                    self.assertEqual((response.status_code, response.content), (204, b''))
                    self.assertEqual(self.commit_count - commits, 1)
                    self.assertIsNotNone(self.lock)
                    self.assertEqual(self.sql[self.post_commit_start:], [])
                finally:
                    self.hold_after_commit = False; self.release_lock()
                actual = self.rows()
                for table in ('conversation', 'message'):
                    expected_global = [r for r in before[table] if r.get('id') in ('global', 'global-message')]
                    self.assertEqual([r for r in actual[table] if r.get('id') in ('global', 'global-message')], expected_global)
                self.assertEqual([r for r in actual['chapter'] if r['novel_id'] == 'foreign'],
                    [r for r in before['chapter'] if r['novel_id'] == 'foreign'])
                if kind == 'outline':
                    expected = {t: [dict(r) for r in rows] for t, rows in before.items()}
                    expected['outline_node'] = []
                    expected['chapter'][0]['outline_node_id'] = ''
                    # Existing ORM bulk unlink advances the target timestamp.
                    self.assertGreaterEqual(actual['chapter'][0]['updated_at'], before['chapter'][0]['updated_at'])
                    expected['chapter'][0]['updated_at'] = actual['chapter'][0]['updated_at']
                    self.assertEqual(actual, expected)
                elif kind == 'conversation':
                    self.assertFalse(any(r['id'] == 'conversation' for r in actual['conversation']))
                    self.assertFalse(any(r['id'] == 'message' for r in actual['message']))
                    self.assertEqual(actual['chat_action'], [])
                    for table in before:
                        if table not in ('conversation', 'message', 'chat_action'):
                            self.assertEqual(actual[table], before[table])
                else:
                    for rows in actual.values():
                        self.assertFalse(any(r.get('novel_id') == 'work' for r in rows))
                    self.assertFalse(any(r['id'] == 'work' for r in actual['novel']))

    def test_success_still_commits_callers_new_dirty_and_Core_changes_together(self):
        for kind in ('outline', 'conversation', 'novel'):
            case = next(r for r in self.cases if r[0] == kind)
            with self.subTest(kind=kind), self.sessions() as db:
                from sqlalchemy import update
                db.add(models.Novel(id=f'pending-{kind}', title='调用方共同创建😀'))
                foreign = db.get(models.Novel, 'foreign')
                foreign.title = f'调用方共同修改-{kind}'
                db.execute(update(models.Chapter).where(models.Chapter.id == 'foreign-chapter')
                    .values(directive=f'调用方Core指令-{kind}'))
                start = self.commit_count
                case[3](db)
                self.assertEqual(self.commit_count - start, 1)
                actual = self.rows()
                self.assertTrue(any(r['id'] == f'pending-{kind}' for r in actual['novel']))
                self.assertEqual(next(r for r in actual['novel'] if r['id'] == 'foreign')['title'], f'调用方共同修改-{kind}')
                self.assertEqual(next(r for r in actual['chapter'] if r['id'] == 'foreign-chapter')['directive'], f'调用方Core指令-{kind}')

    def test_missing_and_outline_children_rejections_preserve_caller_transaction(self):
        with self.sessions() as db:
            pending = models.Novel(id='pending', title='未提交')
            db.add(pending)
            for operation in (lambda: repos.NovelRepo(db).delete('missing'),
                lambda: repos.OutlineRepo(db).delete('work', 'missing'),
                lambda: repos.ConversationRepo(db).delete('missing')):
                with self.assertRaises(HTTPException) as error:
                    operation()
                self.assertEqual(error.exception.status_code, 404)
                self.assertIn(pending, db.new)
            db.add(models.OutlineNode(id='child', novel_id='work', parent_id='node', level='chapter'))
            db.flush()
            with self.assertRaises(HTTPException) as error:
                repos.OutlineRepo(db).delete('work', 'node')
            self.assertEqual(error.exception.status_code, 400)
            self.assertTrue(db.in_transaction())
            self.assertIsNotNone(db.get(models.Novel, 'pending'))
            db.rollback()
        self.assertEqual(self.rows(), self.before)


if __name__ == '__main__':
    unittest.main()
