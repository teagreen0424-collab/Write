"""前章作者来源：实际HTTP改稿、模型等待、精确源值与原动作回放。"""
from threading import Event, Thread
import hashlib
import json
import unittest
from unittest.mock import patch

from sqlalchemy import event

from app.application import writing
from app.infrastructure import models
import test_generated_chapter_transactions as transactions


class GeneratedChapterSourceTest(unittest.TestCase):
    setUp = transactions.GeneratedChapterTransactionTest.setUp
    rows = transactions.GeneratedChapterTransactionTest.rows
    request = transactions.GeneratedChapterTransactionTest.request
    paused_generation = transactions.GeneratedChapterTransactionTest.paused_generation
    assert_conflict = transactions.GeneratedChapterTransactionTest.assert_conflict

    def previous(self):
        return next(row for row in self.rows()["chapter"] if row["id"] == "prior")

    def test_each_author_field_change_during_native_model_wait_conflicts_exactly(self):
        for path in (self.ordinary, self.chat):
            for field in ("title", "content", "directive"):
                with self.subTest(path=path, field=field):
                    # 每个旧实现反例也重新使用真正的prior，避免前一失败已落章改变后续来源。
                    with self.sessions() as db:
                        db.execute(models.ChatAction.__table__.delete())
                        db.execute(models.Chapter.__table__.delete().where(models.Chapter.novel_id == "work", models.Chapter.id != "prior"))
                        db.commit()
                    original = self.previous()
                    value = original[field] + " \u00a0😀\0\r\n"
                    def author_save():
                        response = self.client.put(self.base + "/prior", json={field: value})
                        self.assertEqual(response.status_code, 200)
                    self.assert_conflict(self.paused_generation(path, author_save))
                    self.assertEqual(self.previous()[field], value)
                    self.assertEqual(max(self.rows()["chapter_revision"], key=lambda row: row["revision_no"])[field], original[field])
                    self.assertEqual(len([row for row in self.rows()["chapter"] if row["novel_id"] == "work"]), 1)
                    self.assertEqual(self.rows()["chat_action"], [])

    def test_no_op_author_save_does_not_conflict_or_create_revision(self):
        source = {key: self.previous()[key] for key in ("title", "content", "directive")}
        values = self.paused_generation(self.ordinary, lambda: self.assertEqual(
            self.client.put(self.base + "/prior", json=source).status_code, 200))
        self.assertEqual(values[-1]["type"], "done", values)
        self.assertEqual(self.rows()["chapter_revision"], [])
        self.assertEqual({key: self.previous()[key] for key in source}, source)

    def test_summary_status_scores_and_timestamp_changes_do_not_conflict(self):
        original = {key: self.previous()[key] for key in ("title", "content", "directive")}
        def metadata_save():
            self.assertEqual(self.client.put(self.base + "/prior", json={"summary": "作者单独校正摘要", "status": "finalized"}).status_code, 200)
            with self.sessions() as db:
                db.execute(models.Chapter.__table__.update().where(models.Chapter.id == "prior").values(
                    tension_score=6.5, drift_score=.35, updated_at="2026-10-07T11:00:00+00:00"))
                db.commit()
        values = self.paused_generation(self.ordinary, metadata_save)
        self.assertEqual(values[-1]["type"], "done", values)
        self.assertEqual({key: self.previous()[key] for key in original}, original)
        self.assertEqual(self.previous()["summary"], "作者单独校正摘要")
        self.assertEqual(self.previous()["tension_score"], 6.5)
        self.assertEqual(self.rows()["chapter_revision"], [])

    def outline_gap(self):
        with self.sessions() as db:
            db.add_all([models.OutlineNode(id="node1", novel_id="work", level="chapter", title="第一章", sort_order=1),
                models.OutlineNode(id="node2", novel_id="work", level="chapter", title="补第二章", sort_order=2),
                models.OutlineNode(id="node3", novel_id="work", level="chapter", title="第三章", sort_order=3)])
            db.execute(models.Chapter.__table__.update().where(models.Chapter.id == "prior").values(outline_node_id="node1"))
            db.add(models.Chapter(id="future", novel_id="work", seq=2, outline_node_id="node3", title="未使用的后章", content="绝不能当实际前章的未来正文", directive="后章约束"))
            db.commit()

    def outline_generation(self, mutation):
        entered, release, responses, prompts = Event(), Event(), [], []
        def provider(messages, **kwargs):
            prompts.append(messages)
            entered.set()
            self.assertTrue(release.wait(5))
            yield self.body
        with patch.object(writing.llm, "stream_complete", side_effect=provider):
            worker = Thread(target=lambda: responses.append(self.request(self.ordinary, outline_node_id="node2")))
            worker.start()
            self.assertTrue(entered.wait(5))
            try:
                mutation()
            finally:
                release.set()
                worker.join(8)
        self.assertFalse(worker.is_alive())
        prompt = prompts[0][1]["content"]
        self.assertIn("原前章事实", prompt)
        self.assertNotIn("绝不能当实际前章的未来正文", prompt)
        return transactions.frames(responses[0].text)

    def test_actual_outline_predecessor_not_latest_author_change_conflicts(self):
        self.outline_gap()
        def author_save():
            self.assertEqual(self.client.put(self.base + "/prior", json={"content": "作者撤回原前章事实"}).status_code, 200)
        self.assert_conflict(self.outline_generation(author_save))
        self.assertEqual(self.previous()["content"], "作者撤回原前章事实")
        self.assertEqual(len([row for row in self.rows()["chapter"] if row["novel_id"] == "work"]), 2)

    def test_unused_future_chapter_author_change_does_not_conflict(self):
        self.outline_gap()
        def unused_save():
            self.assertEqual(self.client.put(self.base + "/future", json={"content": "作者明确修改未使用的未来章"}).status_code, 200)
        values = self.outline_generation(unused_save)
        self.assertEqual(values[-1]["type"], "done", values)
        self.assertEqual((values[-1]["chapter"]["seq"], values[-1]["chapter"]["outline_node_id"]), (3, "node2"))
        self.assertEqual(self.previous()["content"], "原前章事实")

    def test_actual_outline_predecessor_deleted_without_latest_change_conflicts(self):
        self.outline_gap()
        self.assert_conflict(self.outline_generation(lambda: self.assertEqual(self.client.delete(self.base + "/prior").status_code, 204)))
        self.assertEqual([row["id"] for row in self.rows()["chapter"] if row["novel_id"] == "work"], ["future"])
        self.assertEqual(self.rows()["chat_action"], [])

    def test_actual_predecessor_moved_to_foreign_scope_without_latest_change_conflicts(self):
        self.outline_gap()
        def moved():
            with self.sessions() as db:
                db.execute(models.Chapter.__table__.update().where(models.Chapter.id == "prior").values(novel_id="foreign"))
                db.commit()
        self.assert_conflict(self.outline_generation(moved))
        self.assertEqual(self.previous()["novel_id"], "foreign")
        self.assertEqual([row["id"] for row in self.rows()["chapter"] if row["novel_id"] == "work"], ["future"])

    def test_no_previous_first_chapter_does_not_capture_foreign_author(self):
        self.assertEqual(self.client.delete(self.base + "/prior").status_code, 204)
        values = self.paused_generation(self.ordinary, lambda: self.assertEqual(self.client.put(
            "/api/v1/novels/foreign/chapters/foreign-chapter", json={"content": "他作作者改稿"}).status_code, 200))
        self.assertEqual(values[-1]["type"], "done", values)
        self.assertEqual(values[-1]["chapter"]["seq"], 1)

    def test_fresh_explicit_generation_after_author_save_reads_new_prompt(self):
        updated = {"title": "新前章标题😀", "content": "精确新前章正文 \0\r\n ", "directive": "新的前章指令 "}
        self.assertEqual(self.client.put(self.base + "/prior", json=updated).status_code, 200)
        with patch.object(writing.llm, "stream_complete", return_value=iter([self.body])) as provider:
            values = transactions.frames(self.request(self.ordinary).text)
        prompt = provider.call_args.args[0][1]["content"]
        self.assertIn(updated["title"], prompt)
        self.assertIn(updated["content"], prompt)
        self.assertNotIn("原前章事实", prompt)
        self.assertEqual(values[-1]["type"], "done", values)
        self.assertEqual({key: self.previous()[key] for key in updated}, updated)

    def test_readonly_cached_no_autoflush_generation_captures_actual_current_previous(self):
        with self.sessions(autoflush=True) as db:
            cached = db.get(models.Chapter, "prior")
            self.assertEqual(cached.content, "原前章事实")
            self.assertEqual(self.client.put(self.base + "/prior", json={"content": "另HTTP真实当前稿"}).status_code, 200)
            with db.no_autoflush, patch.object(writing.llm, "stream_complete", return_value=iter([self.body])) as provider:
                values = [json.loads(line[5:]) for line in writing.write_chapter(db, "work")]
            self.assertIn("另HTTP真实当前稿", provider.call_args.args[0][1]["content"])
            self.assertEqual(values[-1]["type"], "done", values)
            self.assertFalse(db.dirty or db.new or db.deleted)

    def test_inflight_same_assistant_replays_winner_before_deleted_previous_check(self):
        entered_a, entered_b, release_a, release_b, results = Event(), Event(), Event(), Event(), {}
        def provider(messages, **kwargs):
            first = "先完成意图A" in messages[1]["content"]
            (entered_a if first else entered_b).set()
            self.assertTrue((release_a if first else release_b).wait(5))
            yield self.body if first else "另一在途候选，应只读返回原动作"
        def execute(label):
            results[label] = self.request(self.chat, directive=label)
        with patch.object(writing.llm, "stream_complete", side_effect=provider) as model:
            first = Thread(target=execute, args=("先完成意图A",))
            second = Thread(target=execute, args=("后完成意图B",))
            first.start()
            self.assertTrue(entered_a.wait(5))
            second.start()
            self.assertTrue(entered_b.wait(5))
            release_a.set()
            first.join(8)
            self.assertFalse(first.is_alive())
            saved = transactions.frames(results["先完成意图A"].text)[-1]
            self.assertEqual(saved["type"], "done")
            self.assertEqual(self.client.delete(self.base + "/prior").status_code, 204)
            release_b.set()
            second.join(8)
            self.assertFalse(second.is_alive())
            replay = transactions.frames(results["后完成意图B"].text)[-1]
            self.assertEqual(replay, saved)
            self.assertEqual(model.call_count, 2)
        self.assertEqual(len([row for row in self.rows()["chapter"] if row["novel_id"] == "work"]), 1)
        self.assertEqual(len(self.rows()["chat_action"]), 1)

    def test_completed_assistant_receipt_replay_survives_previous_author_change_or_delete(self):
        with patch.object(writing.llm, "stream_complete", return_value=iter([self.body])):
            saved = transactions.frames(self.request(self.chat).text)[-1]
        original_check = saved["consistency"]
        self.assertEqual({key: original_check[key] for key in ("status", "subject", "novel_id", "chapter_id", "content_sha256", "offset_unit", "conflicts")},
            {"status": "completed", "subject": "saved_chapter", "novel_id": "work", "chapter_id": saved["chapter"]["id"],
             "content_sha256": hashlib.sha256(saved["chapter"]["content"].encode("utf-8")).hexdigest(),
             "offset_unit": "unicode_codepoint", "conflicts": []})
        self.assertEqual(self.client.put(self.base + "/prior", json={"content": "已确认动作之后前章改稿"}).status_code, 200)
        self.assertEqual(self.client.delete(self.base + "/prior").status_code, 204)
        before = self.rows()
        with patch.object(writing.llm, "stream_complete") as provider:
            replay = transactions.frames(self.request(self.chat).text)
        provider.assert_not_called()
        self.assertEqual(len(replay), 1)
        self.assertEqual(set(replay[0]), {"type", "chapter", "consistency"})
        self.assertEqual({key: replay[0][key] for key in ("type", "chapter")}, {key: saved[key] for key in ("type", "chapter")})
        check = replay[0]["consistency"]
        self.assertEqual({key: check[key] for key in ("status", "subject", "novel_id", "chapter_id", "content_sha256", "offset_unit", "conflicts")},
            {"status": "skipped", "subject": "existing_action", "novel_id": "work", "chapter_id": saved["chapter"]["id"],
             "content_sha256": hashlib.sha256(saved["chapter"]["content"].encode("utf-8")).hexdigest(),
             "offset_unit": "unicode_codepoint", "conflicts": []})
        self.assertTrue(check["warnings"])
        self.assertEqual(self.rows(), before)

    def test_completed_assistant_deleted_target_remains_missing_without_regeneration(self):
        with patch.object(writing.llm, "stream_complete", return_value=iter([self.body])):
            saved = transactions.frames(self.request(self.chat).text)[-1]
        self.assertEqual(self.client.delete(self.base + "/" + saved["chapter"]["id"]).status_code, 204)
        self.assertEqual(self.client.delete(self.base + "/prior").status_code, 204)
        before = self.rows()
        with patch.object(writing.llm, "stream_complete") as provider:
            values = transactions.frames(self.request(self.chat).text)
        provider.assert_not_called()
        self.assertEqual([row["type"] for row in values], ["error"])
        self.assertIn("原聊天动作对应章节已删除", values[0]["message"])
        self.assertEqual(self.rows(), before)

    def test_actual_source_check_is_locked_until_generation_commit_before_author_put(self):
        checked, writer_attempted, writer_result = Event(), Event(), []
        worker = Thread(target=lambda: writer_result.append(self.client.put(
            self.base + "/prior", json={"content": "作者在生成提交之后保存的新稿"})))

        def before(_connection, _cursor, statement, *_args):
            if checked.is_set() and statement == "BEGIN IMMEDIATE":
                writer_attempted.set()

        def after(connection, _cursor, statement, *_args):
            if " ".join(statement.split()).startswith("SELECT chapter.title, chapter.content, chapter.directive "):
                self.assertTrue(connection.connection.driver_connection.in_transaction)
                checked.set()
                worker.start()
                self.assertTrue(writer_attempted.wait(3))
                # 作者的真实PUT已尝试BEGIN，但同章改稿只能在生成短事务释放后提交。

        event.listen(self.engine, "before_cursor_execute", before)
        event.listen(self.engine, "after_cursor_execute", after)
        try:
            with patch.object(writing.llm, "stream_complete", return_value=iter([self.body])):
                values = transactions.frames(self.request(self.ordinary).text)
            self.assertTrue(checked.is_set())
            worker.join(8)
            self.assertFalse(worker.is_alive())
            self.assertEqual(values[-1]["type"], "done", values)
            self.assertEqual(writer_result[0].status_code, 200)
            self.assertEqual(self.previous()["content"], "作者在生成提交之后保存的新稿")
            self.assertEqual(self.rows()["chapter_revision"][0]["content"], "原前章事实")
            self.assertEqual(len([row for row in self.rows()["chapter"] if row["novel_id"] == "work"]), 2)
        finally:
            event.remove(self.engine, "before_cursor_execute", before)
            event.remove(self.engine, "after_cursor_execute", after)
