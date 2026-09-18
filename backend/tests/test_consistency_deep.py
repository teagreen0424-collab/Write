"""阶段七单元测试：一致性语义层 deep_check（切块 / 跳过逻辑 / LLM 复核）。"""
import json
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.application import consistency as consistency_uc
from app.infrastructure import models
from app.infrastructure import repository as repos

engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
models.Base.metadata.create_all(bind=engine)
Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class SplitChunksTest(unittest.TestCase):
    def test_offsets_are_contiguous_substrings(self):
        content = "第一段内容。\n\n第二段内容。\n\n第三段内容。"
        chunks = consistency_uc._split_chunks(content)
        self.assertEqual(len(chunks), 3)
        for c in chunks:
            self.assertEqual(content[c["start"] : c["start"] + len(c["text"])], c["text"])
        self.assertEqual([c["start"] for c in chunks], [0, 8, 16])

    def test_long_paragraph_hard_split(self):
        content = "长" * 3000
        chunks = consistency_uc._split_chunks(content)
        self.assertEqual([len(c["text"]) for c in chunks], [800, 800, 1400])
        self.assertEqual([c["start"] for c in chunks], [0, 800, 1600])


class DeepCheckTest(unittest.TestCase):
    def setUp(self):
        self.db = Session()
        novel = repos.NovelRepo(self.db).create(title="测试作品", genre="玄幻", summary="")
        self.novel_id = novel.id
        repos.CharacterRepo(self.db).create(self.novel_id, name="林越")

    def tearDown(self):
        self.db.close()

    def test_skip_rule_hit_and_nameless_chunks(self):
        repos.ForeshadowingRepo(self.db).create(
            self.novel_id, content="身世之谜", status="consumed"
        )
        content = "林越忽然想起身世之谜。\n\n无人名的空白段落。\n\n林越继续前行。"
        with patch(
            "app.application.consistency.llm.complete", return_value='{"conflicts": []}'
        ):
            result = consistency_uc.deep_check(self.db, self.novel_id, content)

        self.assertEqual(result["chunks_checked"], 1)
        self.assertEqual(result["chunks_skipped"], 2)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["conflicts"], [])

    def test_conflict_fields_mapping(self):
        content = "林越站在山巅之上。"
        fake = json.dumps(
            {
                "conflicts": [
                    {
                        "type": "人设矛盾",
                        "excerpt": "林越站在山巅之上",
                        "reason": "设定中林越恐高",
                        "severity": "high",
                    }
                ]
            }
        )
        with patch("app.application.consistency.llm.complete", return_value=fake):
            result = consistency_uc.deep_check(self.db, self.novel_id, content)

        self.assertEqual(result["chunks_checked"], 1)
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(
            result["conflicts"][0],
            {
                "rule": "semantic",
                "type": "人设矛盾",
                "excerpt": "林越站在山巅之上",
                "reason": "设定中林越恐高",
                "severity": "high",
                "chunk_index": 0,
            },
        )

    def test_truncated_at_max_chunks(self):
        content = "\n\n".join(f"林越在第{i}段中出场。" for i in range(1, 10))
        with patch(
            "app.application.consistency.llm.complete", return_value='{"conflicts": []}'
        ) as mocked:
            result = consistency_uc.deep_check(self.db, self.novel_id, content)

        self.assertEqual(result["chunks_checked"], consistency_uc.MAX_CHUNKS)
        self.assertEqual(result["chunks_skipped"], 0)
        self.assertTrue(result["truncated"])
        self.assertEqual(mocked.call_count, consistency_uc.MAX_CHUNKS)

    def test_single_chunk_failure_does_not_break(self):
        content = "林越站在山巅之上。\n\n林越低头沉默。"
        with patch(
            "app.application.consistency.llm.complete",
            side_effect=RuntimeError("模型不可用"),
        ):
            result = consistency_uc.deep_check(self.db, self.novel_id, content)

        self.assertEqual(result["chunks_checked"], 2)
        self.assertEqual(result["conflicts"], [])


if __name__ == "__main__":
    unittest.main()
