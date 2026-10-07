"""云端批量嵌入必须按输入索引对应，异常响应不可污染设定召回。"""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.application import setting_index
from app.infrastructure import models, repository as repos
from app.infrastructure.ai.embedding import EmbeddingService


class CloudEmbeddingBatchTest(unittest.TestCase):
    def setUp(self):
        settings = SimpleNamespace(
            embedding_service="openai", embedding_api_key="isolated-test",
            embedding_base_url="https://embedding.invalid", embedding_model="isolated",
            embedding_model_path="",
        )
        with patch("app.infrastructure.ai.embedding.get_settings", return_value=settings):
            self.service = EmbeddingService()
        for transport in (httpx.HTTPTransport, httpx.AsyncHTTPTransport):
            method = "handle_request" if transport is httpx.HTTPTransport else "handle_async_request"
            guard = patch.object(transport, method, side_effect=AssertionError("禁止外部 HTTP"))
            guard.start()
            self.addCleanup(guard.stop)

    @staticmethod
    def response(payload):
        return httpx.Response(
            200, request=httpx.Request("POST", "https://embedding.invalid/embeddings"),
            json=payload,
        )

    def embed_payload(self, payload, texts=None):
        with patch("app.infrastructure.ai.embedding.httpx.post", return_value=self.response(payload)) as post:
            result = self.service._embed_cloud(texts or ["文本甲", "文本乙"])
        return result, post

    def test_indexed_response_is_sorted_by_input_index(self):
        texts = ["文本甲", "文本乙", "文本丙"]
        payload = {"data": [
            {"index": 2, "embedding": [3.0]}, {"index": 0, "embedding": [1.0]},
            {"index": 1, "embedding": [2.0]},
        ]}
        with patch("app.infrastructure.ai.embedding.httpx.post", return_value=self.response(payload)) as post:
            self.assertEqual(self.service._embed_cloud(texts), [[1.0], [2.0], [3.0]])
        self.assertEqual(post.call_args.kwargs["json"]["input"], texts)

    def test_duplicate_negative_out_of_range_and_noninteger_indices_are_rejected(self):
        for indices in ([0, 0], [-1, 1], [0, 2], [False, 1], [0, True], [0.0, 1], ["0", 1], [None, 1]):
            with self.subTest(indices=indices), self.assertRaises(ValueError):
                self.embed_payload({"data": [{"index": index, "embedding": [float(pos)]}
                                              for pos, index in enumerate(indices)]})

    def test_mixed_indexed_and_unindexed_response_is_rejected(self):
        with self.assertRaises(ValueError):
            self.embed_payload({"data": [{"index": 0, "embedding": [1.0]}, {"embedding": [2.0]}]})

    def test_unindexed_compatible_response_keeps_positions(self):
        result, _ = self.embed_payload({"data": [{"embedding": [3.0]}, {"embedding": [7.0]}]})
        self.assertEqual(result, [[3.0], [7.0]])

    def test_short_and_extra_responses_are_rejected_with_or_without_indices(self):
        for count in (1, 3):
            for indexed in (True, False):
                items = [{"embedding": [float(index)], **({"index": index} if indexed else {})}
                         for index in range(count)]
                with self.subTest(count=count, indexed=indexed), self.assertRaises(ValueError):
                    self.embed_payload({"data": items})

    def test_invalid_response_structure_is_rejected(self):
        for payload in ([], {}, {"data": None}, {"data": {}}, {"data": [1, 2]},
                        {"data": [{"index": 0}, {"index": 1, "embedding": [2.0]}]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.embed_payload(payload)

    def test_empty_input_never_sends_http(self):
        with patch("app.infrastructure.ai.embedding.httpx.post") as post:
            self.assertEqual(self.service._embed_cloud([]), [])
        post.assert_not_called()

    def isolated_settings(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(engine.dispose)
        models.Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine, autoflush=False)()
        self.addCleanup(db.close)
        nid = repos.NovelRepo(db).create(title="当前作品").id
        other = repos.NovelRepo(db).create(title="另一作品").id
        db.add_all([models.Character(id="a", novel_id=nid, name="人物甲"),
                    models.Character(id="b", novel_id=nid, name="人物乙")])
        db.flush()
        db.add_all([
            models.MemoryChunk(novel_id=nid, kind="setting", embedding_id="a", text="旧甲", vector=[9.0]),
            models.MemoryChunk(novel_id=nid, kind="setting", embedding_id="b", text="旧乙", vector=[8.0]),
            models.MemoryChunk(novel_id=nid, kind="chapter_slice", embedding_id="a", text="正文切片", vector=[7.0]),
            models.MemoryChunk(novel_id=other, kind="setting", embedding_id="a", text="另一作品设定", vector=[6.0]),
        ])
        db.commit()
        return db, nid, other

    def test_rebuild_with_real_cloud_adapter_keeps_text_and_vector_correspondence(self):
        db, nid, other = self.isolated_settings()

        def reversed_response(*_args, **kwargs):
            texts = kwargs["json"]["input"]
            return self.response({"data": [
                {"index": index, "embedding": [float(sum(map(ord, text))), 1.0]}
                for index, text in reversed(list(enumerate(texts)))
            ]})

        with patch.object(setting_index, "embedding", self.service), \
                patch.object(self.service, "embed", side_effect=self.service._embed_cloud), \
                patch("app.infrastructure.ai.embedding.httpx.post", side_effect=reversed_response):
            self.assertEqual(setting_index.rebuild_settings_index(db, nid), {"indexed": 2, "failed": 0})
        rows = db.query(models.MemoryChunk).filter_by(novel_id=nid, kind="setting").all()
        self.assertEqual({row.embedding_id for row in rows}, {"a", "b"})
        for row in rows:
            self.assertEqual(row.vector, [float(sum(map(ord, row.text))), 1.0])
        self.assertEqual(db.query(models.MemoryChunk).filter_by(novel_id=other).one().text, "另一作品设定")
        self.assertEqual(db.query(models.MemoryChunk).filter_by(novel_id=nid, kind="chapter_slice").one().text, "正文切片")

    def test_invalid_cloud_mapping_invalidates_only_target_setting_indices(self):
        invalid_responses = (
            [{"index": 0, "embedding": [1.0]}, {"index": 0, "embedding": [2.0]}],
            [{"index": 0, "embedding": [1.0]}, {"embedding": [2.0]}],
            [{"index": False, "embedding": [1.0]}, {"index": 1, "embedding": [2.0]}],
            [{"index": 0, "embedding": [1.0]}],
            [{"embedding": [1.0]}, {"embedding": [2.0]}, {"embedding": [3.0]}],
        )
        for items in invalid_responses:
            with self.subTest(items=items):
                db, nid, other = self.isolated_settings()
                with patch.object(setting_index, "embedding", self.service), \
                        patch.object(self.service, "embed", side_effect=self.service._embed_cloud), \
                        patch("app.infrastructure.ai.embedding.httpx.post", return_value=self.response({"data": items})):
                    self.assertEqual(setting_index.rebuild_settings_index(db, nid), {"indexed": 0, "failed": 2})
                self.assertEqual(db.query(models.MemoryChunk).filter_by(novel_id=nid, kind="setting").count(), 0)
                self.assertEqual(db.query(models.Character).filter_by(novel_id=nid).count(), 2)
                self.assertEqual(db.query(models.MemoryChunk).filter_by(novel_id=other).one().text, "另一作品设定")
                self.assertEqual(db.query(models.MemoryChunk).filter_by(novel_id=nid, kind="chapter_slice").one().text, "正文切片")


if __name__ == "__main__":
    unittest.main()
