"""检查编辑稿的字数使用完整输入，范围验证和计数均不改数据库。"""
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.infrastructure import models
from app.infrastructure.db import get_db
from app.infrastructure.repository import ChapterRepo, NovelRepo
from app.interfaces.api.v1 import chapters
from app.interfaces.schemas import StoryCheckResult


class StoryWordLimitsApiTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        models.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.novel = NovelRepo(self.db).create(title='范围核对')
        self.chapter = ChapterRepo(self.db).create(self.novel.id, seq=1, title='原章', content='原稿')
        app = FastAPI()
        app.include_router(chapters.router, prefix='/api/v1')
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)
        self.path = f'/api/v1/novels/{self.novel.id}/chapters/{self.chapter.id}/story-check'

    def tearDown(self):
        self.client.close()
        self.db.close()
        self.engine.dispose()

    def snapshot(self):
        return {table.name: [tuple(row) for row in self.db.execute(select(table)).all()]
                for table in models.Base.metadata.sorted_tables}

    def test_invalid_limits_are_rejected_before_any_model_call(self):
        before = self.snapshot()
        invalid = ({}, {'min_words': True}, {'max_words': 4.5}, {'min_words': 0},
                   {'max_words': 1_000_001}, {'min_words': 8, 'max_words': 4}, {'max_words': 5, 'extra': 1})
        with patch.object(chapters.story_review_uc, 'review_story') as review:
            for limits in invalid:
                with self.subTest(limits=limits):
                    response = self.client.post(self.path, json={'word_limits': limits})
                    self.assertEqual(response.status_code, 422, response.text)
            review.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_unicode_edit_count_is_independent_of_incomplete_model_result(self):
        before = self.snapshot()
        edited = '汉字\n A🙂\t！'
        with patch.object(chapters.story_review_uc, 'review_story', return_value=StoryCheckResult(review_complete=False)) as review:
            response = self.client.post(self.path, json={'content': edited, 'directive': '',
                                                       'word_limits': {'min_words': 5, 'max_words': 5}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['word_count'], {'count': 5, 'min_words': 5, 'max_words': 5,
                                                        'within_limits': True, 'warnings': []})
        self.assertFalse(response.json()['review_complete'])
        self.assertEqual(review.call_args.args[2], edited)
        self.assertEqual(self.snapshot(), before)

    def test_over_limit_warns_without_claiming_unsaved_edit_was_saved(self):
        before = self.snapshot()
        edited = '完整编辑稿' * 2200
        with patch.object(chapters.story_review_uc, 'review_story', return_value=StoryCheckResult()):
            response = self.client.post(self.path, json={'content': edited, 'word_limits': {'max_words': 1600}})
        self.assertEqual(response.status_code, 200, response.text)
        count = response.json()['word_count']
        self.assertEqual(count['count'], len(edited))
        self.assertFalse(count['within_limits'])
        self.assertTrue(count['warnings'])
        self.assertNotIn('已保存', count['warnings'][0])
        self.assertEqual(self.snapshot(), before)

    def test_old_request_gets_count_without_implicit_bounds(self):
        with patch.object(chapters.story_review_uc, 'review_story', return_value=StoryCheckResult()):
            response = self.client.post(self.path, json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['word_count'], {'count': 2, 'min_words': None, 'max_words': None,
                                                        'within_limits': None, 'warnings': []})


if __name__ == '__main__':
    unittest.main()
