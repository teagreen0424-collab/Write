"""自动驾驶补章在真实 SQLite 事务中插回大纲位置，保留原稿与派生数据。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.application import auto_pilot
from app.infrastructure import models
from app.infrastructure import repository as repos
from app.infrastructure.ai.llm_provider import LLMStreamIncompleteError


def _truncated():
    yield "保留给作者的未完成稿。"
    raise LLMStreamIncompleteError("达到输出上限", code="output_truncated", finish_reason="length")


class AutoPilotOrderTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = (Path(folder.name) / "autopilot-order.db").as_posix()
        self.engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
        models.Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.session_factory()
        self.addCleanup(self.db.close)
        self.novel_id = repos.NovelRepo(self.db).create(title="补章排序验收").id
        for mocked in (
            patch.object(auto_pilot, "registry", auto_pilot.AutoPilotRegistry()),
            patch.object(auto_pilot, "build_context", return_value="测试上下文"),
            patch.object(auto_pilot, "consistency_check", return_value={"conflicts": []}),
            patch.object(auto_pilot, "score_tension", return_value=5.0),
            patch.object(auto_pilot, "check_drift", return_value=None),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)

    def _nodes(self, count):
        return [repos.OutlineRepo(self.db).create(
            self.novel_id, title=f"第{index}章", level="chapter", sort_order=index,
        ) for index in range(1, count + 1)]

    def _chapter(self, node=None, seq=None, **values):
        return repos.ChapterRepo(self.db).create(
            self.novel_id, outline_node_id=node.id if node else "", seq=seq,
            title=node.title if node else "作者自建章", content="作者的原始正文。",
            **values,
        )

    def _run(self, outputs=None):
        kwargs = {"side_effect": outputs} if outputs is not None else {
            "side_effect": lambda *a, **kw: iter(["完整的新正文。"]),
        }
        with patch.object(auto_pilot.llm, "stream_complete", **kwargs) as stream:
            emitted = [json.loads(frame[5:].strip()) for frame in auto_pilot.run_autopilot(
                self.novel_id, session_factory=self.session_factory,
            )]
        self.assertFalse(auto_pilot.registry.is_running(self.novel_id))
        self.assertEqual(emitted[-1]["type"], "done")
        return emitted, stream.call_count

    def _rows(self):
        self.db.expire_all()
        return {
            table.name: sorted(
                [dict(row) for row in self.db.execute(select(table)).mappings()],
                key=lambda row: row["id"],
            ) for table in models.Base.metadata.sorted_tables
            if table.name != "autopilot_draft"  # 正式资料不变；半稿恢复由专门测试覆盖。
        }

    def _chapters(self):
        self.db.expire_all()
        chapters = repos.ChapterRepo(self.db).list(self.novel_id)
        self.assertEqual(len({chapter.seq for chapter in chapters}), len(chapters))
        return chapters

    def _assert_preserved(self, before, after, shifted_ids=()):
        old_ids = {row["id"] for row in before["chapter"]}
        old_after = {row["id"]: row for row in after["chapter"] if row["id"] in old_ids}
        for original in before["chapter"]:
            expected = {**original, "seq": original["seq"] + (original["id"] in shifted_ids)}
            self.assertEqual(old_after[original["id"]], expected)
        for name in before:
            if name != "chapter":
                self.assertEqual(after[name], before[name], name)

    def test_retry_middle_failure_inserts_before_successor_and_preserves_all_author_data(self):
        nodes = self._nodes(3)
        emitted, calls = self._run((iter(["第一章原稿。"]), _truncated(), iter(["第三章原稿。"])))
        self.assertEqual(calls, 3)
        self.assertEqual(emitted[-1]["status"], "incomplete")
        first, third = self._chapters()
        for chapter in (first, third):
            repos.ChapterRepo(self.db).update(
                self.novel_id, chapter.id, status="finalized", summary="不可改写的摘要。",
                tension_score=8.1, drift_score=0.95, directive="作者的原始要求。",
            )
            repos.ChapterRevisionRepo(self.db).add_snapshot(chapter, "manual_save", 9)
            self.db.add(models.MemoryChunk(
                novel_id=self.novel_id, source_chapter_id=chapter.id,
                text="原正文记忆", vector=[1.0, 0.0], embedding_id="existing-vector",
            ))
        self.db.add(models.Foreshadowing(
            novel_id=self.novel_id, content="不可改变的伏笔关联",
            planted_chapter_id=first.id, consumed_chapter_id=third.id,
        ))
        self.db.commit()
        before = self._rows()

        emitted, calls = self._run()
        self.assertEqual(calls, 1)
        self.assertEqual([chapter.outline_node_id for chapter in self._chapters()], [node.id for node in nodes])
        self.assertEqual([chapter.seq for chapter in self._chapters()], [1, 2, 3])
        self._assert_preserved(before, self._rows(), {third.id})
        finished = next(event for event in emitted if event["type"] == "chapter_done")
        self.assertEqual((finished["seq"], finished["node_id"]), (2, nodes[1].id))
        self.assertEqual((emitted[-1]["status"], emitted[-1]["generated"], emitted[-1]["skipped"]),
                         ("completed", 1, 2))

    def test_all_existing_skips_without_reordering_even_if_author_order_differs(self):
        nodes = self._nodes(3)
        self._chapter(nodes[0], seq=20)
        self._chapter(nodes[1], seq=10)
        self._chapter(nodes[2], seq=30)
        self._chapter(seq=40)
        before = self._rows()
        emitted, calls = self._run()
        self.assertEqual(calls, 0)
        self.assertEqual(emitted[-1]["status"], "completed")
        self.assertEqual(self._rows(), before)

    def test_multi_volume_and_act_order_locates_successor_across_parent_boundaries(self):
        repo = repos.OutlineRepo(self.db)
        late_volume = repo.create(self.novel_id, title="第二卷", level="volume", sort_order=20)
        early_volume = repo.create(self.novel_id, title="第一卷", level="volume", sort_order=10)
        later_act = repo.create(self.novel_id, title="后幕", level="act", parent_id=early_volume.id, sort_order=9)
        early_act = repo.create(self.novel_id, title="前幕", level="act", parent_id=early_volume.id, sort_order=2)
        last = repo.create(self.novel_id, title="尾章", level="chapter", parent_id=late_volume.id, sort_order=0)
        middle = repo.create(self.novel_id, title="补章", level="chapter", parent_id=later_act.id, sort_order=0)
        first = repo.create(self.novel_id, title="首章", level="chapter", parent_id=early_act.id, sort_order=100)
        self._chapter(first, seq=1)
        self._chapter(last, seq=2)
        emitted, calls = self._run()
        self.assertEqual(calls, 1)
        self.assertEqual([chapter.outline_node_id for chapter in self._chapters()], [first.id, middle.id, last.id])
        self.assertEqual(emitted[-1]["status"], "completed")

    def test_equal_sort_order_uses_existing_outline_created_at_and_id_tie_breaks(self):
        repo = repos.OutlineRepo(self.db)
        shared_time = "2026-10-06T00:00:00+00:00"
        last = repo.create(
            self.novel_id, id="tie-z", title="末章", level="chapter",
            sort_order=1, created_at=shared_time,
        )
        first = repo.create(
            self.novel_id, id="tie-a", title="首章", level="chapter",
            sort_order=1, created_at=shared_time,
        )
        middle = repo.create(
            self.novel_id, id="tie-m", title="补章", level="chapter",
            sort_order=1, created_at=shared_time,
        )
        self._chapter(first, seq=1)
        self._chapter(last, seq=2)
        self.assertEqual([node.id for node in auto_pilot._ordered_chapter_nodes(self.db, self.novel_id)],
                         [node.id for node in repo.list_all(self.novel_id)])
        self._run()
        self.assertEqual([chapter.outline_node_id for chapter in self._chapters()],
                         [first.id, middle.id, last.id])

    def test_unbound_author_chapters_keep_relative_order_on_both_sides(self):
        nodes = self._nodes(3)
        prefix = self._chapter(seq=1)
        first = self._chapter(nodes[0], seq=2)
        between = self._chapter(seq=3)
        third = self._chapter(nodes[2], seq=4)
        suffix = self._chapter(seq=5)
        before = self._rows()
        self._run()
        chapters = self._chapters()
        self.assertEqual([chapter.id for chapter in chapters if chapter.id in {
            prefix.id, first.id, between.id, third.id, suffix.id,
        }], [prefix.id, first.id, between.id, third.id, suffix.id])
        self.assertEqual([chapter.outline_node_id for chapter in chapters], ["", nodes[0].id, "", nodes[1].id, nodes[2].id, ""])
        self._assert_preserved(before, self._rows(), {third.id, suffix.id})

    def test_no_later_outline_anchor_appends_without_moving_authored_tail(self):
        nodes = self._nodes(2)
        first = self._chapter(nodes[0], seq=4)
        tail = self._chapter(seq=20)
        before = self._rows()
        self._run()
        self.assertEqual([chapter.id for chapter in self._chapters()[:2]], [first.id, tail.id])
        self.assertEqual(self._chapters()[-1].seq, 21)
        self._assert_preserved(before, self._rows())

    def test_duplicate_bindings_are_preserved_as_groups_and_do_not_duplicate_missing_node(self):
        nodes = self._nodes(3)
        self._chapter(nodes[0], seq=1)
        self._chapter(nodes[0], seq=2)
        third_a = self._chapter(nodes[2], seq=3)
        third_b = self._chapter(nodes[2], seq=4)
        before = self._rows()
        self._run()
        self.assertEqual([chapter.outline_node_id for chapter in self._chapters()],
                         [nodes[0].id, nodes[0].id, nodes[1].id, nodes[2].id, nodes[2].id])
        self._assert_preserved(before, self._rows(), {third_a.id, third_b.id})
        after = self._rows()
        emitted, calls = self._run()
        self.assertEqual(calls, 0)
        self.assertEqual(emitted[-1]["status"], "completed")
        self.assertEqual(self._rows(), after)

    def test_inconsistent_existing_anchor_order_reports_failure_without_rearrangement(self):
        nodes = self._nodes(3)
        self._chapter(nodes[0], seq=2)
        self._chapter(nodes[2], seq=1)
        before = self._rows()
        emitted, calls = self._run()
        self.assertEqual(calls, 1)
        error = next(event for event in emitted if event["type"] == "error")
        self.assertEqual((error["code"], error["saved"]), ("autopilot_order_ambiguous", False))
        self.assertIn("前后大纲章", error["message"])
        self.assertEqual(self._rows(), before)
        self.assertEqual(emitted[-1]["status"], "incomplete")

    def test_duplicate_existing_seq_is_not_silently_normalized_or_extended(self):
        nodes = self._nodes(3)
        self._chapter(nodes[0], seq=1)
        self._chapter(nodes[2], seq=1)
        before = self._rows()
        emitted, _ = self._run()
        error = next(event for event in emitted if event["type"] == "error")
        self.assertEqual(error["code"], "autopilot_order_ambiguous")
        self.assertIn("序号重复", error["message"])
        self.assertEqual(self._rows(), before)
        self.assertEqual(emitted[-1]["status"], "incomplete")

    def test_real_insert_failure_rolls_back_seq_shift_and_retry_has_no_duplicate_seq(self):
        nodes = self._nodes(3)
        self._chapter(nodes[0], seq=1)
        third = self._chapter(nodes[2], seq=2)
        before = self._rows()
        self.db.execute(text(
            "CREATE TRIGGER fail_autopilot_insert BEFORE INSERT ON chapter "
            "BEGIN SELECT RAISE(ABORT, 'injected chapter insert failure'); END"
        ))
        self.db.commit()
        emitted, calls = self._run()
        self.assertEqual(calls, 1)
        self.assertEqual(emitted[-1]["status"], "incomplete")
        self.assertEqual(self._rows(), before)
        self.assertEqual([chapter.seq for chapter in self._chapters()], [1, 2])
        self.assertFalse(any(event["type"] == "chapter_done" for event in emitted))
        self.db.execute(text("DROP TRIGGER fail_autopilot_insert"))
        self.db.commit()
        self._run()
        self.assertEqual([chapter.seq for chapter in self._chapters()], [1, 2, 3])
        self._assert_preserved(before, self._rows(), {third.id})

    def test_multiple_missing_nodes_insert_sequentially_without_compacting_existing_gaps(self):
        nodes = self._nodes(5)
        first = self._chapter(nodes[0], seq=3)
        third = self._chapter(nodes[2], seq=10)
        fifth = self._chapter(nodes[4], seq=20)
        before = self._rows()
        emitted, calls = self._run()
        self.assertEqual(calls, 2)
        chapters = self._chapters()
        self.assertEqual([chapter.outline_node_id for chapter in chapters], [node.id for node in nodes])
        self.assertEqual([chapter.seq for chapter in chapters], [3, 10, 11, 21, 22])
        old_rows = {row["id"]: row for row in self._rows()["chapter"]}
        for original in before["chapter"]:
            increment = {first.id: 0, third.id: 1, fifth.id: 2}[original["id"]]
            self.assertEqual(old_rows[original["id"]], {**original, "seq": original["seq"] + increment})
        self.assertEqual(emitted[-1]["status"], "completed")

    def test_sequence_checks_and_shift_are_scoped_to_the_active_novel(self):
        nodes = self._nodes(3)
        self._chapter(nodes[0], seq=1)
        third = self._chapter(nodes[2], seq=2)
        other = repos.NovelRepo(self.db).create(title="不可改写的其它作品")
        for _ in range(2):
            repos.ChapterRepo(self.db).create(other.id, seq=2, title="其它作品章", content="保护正文")
        before = self._rows()
        self._run()
        self.assertEqual([chapter.seq for chapter in self._chapters()], [1, 2, 3])
        self._assert_preserved(before, self._rows(), {third.id})

    def test_author_save_during_generation_prevents_duplicate_outline_binding(self):
        node = self._nodes(1)[0]

        def author_saved(*args, **kwargs):
            yield "尚未提交的模型正文。"
            with self.session_factory() as author_db:
                repos.ChapterRepo(author_db).create(
                    self.novel_id, outline_node_id=node.id, seq=1,
                    title="作者即时保存", content="以作者的正文为准。", status="finalized",
                )

        with patch.object(auto_pilot.llm, "stream_complete", side_effect=author_saved):
            emitted = [json.loads(frame[5:].strip()) for frame in auto_pilot.run_autopilot(
                self.novel_id, session_factory=self.session_factory,
            )]
        chapters = self._chapters()
        self.assertEqual(len(chapters), 1)
        self.assertEqual((chapters[0].content, chapters[0].status), ("以作者的正文为准。", "finalized"))
        error = next(event for event in emitted if event["type"] == "error")
        self.assertIn("避免重复", error["message"])
        self.assertFalse(error["saved"])
        self.assertFalse(any(event["type"] == "chapter_done" for event in emitted))


if __name__ == "__main__":
    unittest.main()
