"""生成提示的完整正文、真实事实投影、原事务与提交后零 SQL 合同。"""
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.application import chat_actions, consistency, writing
from app.infrastructure import models
from app.infrastructure.db import get_db
from app.interfaces.api.v1 import chapters, conversations


def frames(body):
    return [json.loads(line[5:].strip()) for line in body.splitlines() if line.startswith("data:")]


class GeneratedConsistencyTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "rule-check.db"
        self.engine = create_engine(f"sqlite:///{self.path.as_posix()}", connect_args={"check_same_thread": False, "timeout": .2})
        self.addCleanup(self.engine.dispose)
        models.Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=True)
        self.raw = "  😀\x00\r\n谜底。林越知道青云遗孤。结果随后起因。  "
        self.original = "🐾原文段落。其余正文。"
        with self.sessions() as db:
            db.add_all([models.Novel(id="work", title="规则作品"), models.Novel(id="foreign", title="其他作品"),
                models.Chapter(id="prior", novel_id="work", seq=1, title="原章", content=self.original, directive="原指令"),
                models.Conversation(id="thread", novel_id="work", title="来源"),
                models.Message(id="source-a", conversation_id="thread", role="assistant", content="明确构思"),
                models.Foreshadowing(id="clue", novel_id="work", content="谜底", status="consumed"),
                models.Character(id="person", novel_id="work", name="林越", known_info="不知道青云遗孤。"),
                models.TimelineEvent(id="cause", novel_id="work", title="起因"),
                models.TimelineEvent(id="effect", novel_id="work", title="结果", cause_id="cause"),
                models.Foreshadowing(id="foreign-clue", novel_id="foreign", content="原文", status="suspended")])
            db.commit()
        app = FastAPI()
        app.include_router(chapters.router, prefix="/api/v1")
        app.include_router(conversations.router, prefix="/api/v1")

        def dependency():
            with self.sessions() as db:
                yield db

        app.dependency_overrides[get_db] = dependency
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        for module in (chapters, conversations):
            mocked = patch.object(module, "SessionLocal", self.sessions)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.ordinary = "/api/v1/novels/work/chapters/write"
        self.chat = "/api/v1/conversations/thread/messages/source-a/chapter/write"
        self.continue_path = "/api/v1/novels/work/chapters/prior/continue"
        self.rewrite = "/api/v1/novels/work/chapters/prior/rewrite"
        self.trace, self.provider_calls = [], []
        self.evidence = {"native_http": self.trace, "controlled_generation_calls": self.provider_calls}

    def rows(self):
        with self.sessions() as db:
            return {table.name: [dict(row) for row in db.execute(select(table).order_by(*table.primary_key.columns)).mappings()]
                for table in models.Base.metadata.sorted_tables}

    def provider(self, *_args, **_kwargs):
        self.provider_calls.append({"chunks": [self.raw]})
        yield self.raw

    def request(self, path, **payload):
        response = self.client.post(path, json=payload)
        values = frames(response.text)
        self.trace.append({"method": "POST", "path": path, "json": payload, "http": response.status_code, "events": values})
        self.assertEqual(response.status_code, 200)
        return values

    def done(self, values):
        self.assertEqual([row["type"] for row in values].count("done"), 1, values)
        self.assertEqual(values[-1]["type"], "done", values)
        return values[-1]

    def assert_metadata(self, value, content, *, subject="saved_chapter", status="completed", rules=None):
        self.assertIn("consistency", value)
        result = value["consistency"]
        self.assertEqual(set(result), {"status", "novel_id", "chapter_id", "subject", "content_sha256", "offset_unit", "conflicts", "warnings"})
        self.assertEqual((result["status"], result["novel_id"], result["chapter_id"], result["subject"], result["offset_unit"]),
            (status, "work", value["chapter"]["id"], subject, "unicode_codepoint"))
        self.assertEqual(result["content_sha256"], hashlib.sha256(content.encode("utf-8")).hexdigest())
        self.assertIsInstance(result["warnings"], list)
        self.assertTrue(result["warnings"])
        self.assertTrue(all(isinstance(item, str) for item in result["warnings"]))
        if rules is not None:
            self.assertEqual([item["rule"] for item in result["conflicts"]], rules)
        for conflict in result["conflicts"]:
            self.assertEqual(set(conflict), {"rule", "message", "start", "end", "entity_id"})
            self.assertTrue(0 <= conflict["start"] < conflict["end"] <= len(content))
        return result

    def generate(self, path, **payload):
        with patch.object(writing.llm, "stream_complete", side_effect=self.provider):
            return self.done(self.request(path, **payload))

    def test_native_new_chapter_exact_raw_full_content_and_three_codepoint_rules(self):
        value = self.generate(self.ordinary, title="新章")
        result = self.assert_metadata(value, self.raw, rules=["foreshadowing", "known_info", "timeline"])
        self.assertEqual(value["chapter"]["content"], self.raw)
        self.assertEqual([self.raw[item["start"]:item["end"]] for item in result["conflicts"]], ["谜底", "青云遗孤", "结果"])
        self.assertEqual(result["conflicts"][0]["start"], self.raw.index("谜底"))
        self.assertEqual(len(self.provider_calls), 1)
        self.assertEqual([row["content"] for row in self.rows()["chapter"] if row["id"] == value["chapter"]["id"]], [self.raw])

    def test_native_next_chapter_and_chat_each_keep_single_generation_and_saved_fact(self):
        for path, payload in ((self.ordinary, {"previous_chapter_id": "prior"}), (self.chat, {})):
            with self.subTest(path=path):
                value = self.generate(path, **payload)
                self.assert_metadata(value, value["chapter"]["content"], rules=["foreshadowing", "known_info", "timeline"])
        self.assertEqual(len(self.provider_calls), 2)
        self.assertEqual(len(self.rows()["chat_action"]), 1)

    def test_native_continue_checks_original_plus_generated_and_keeps_old_revision(self):
        value = self.generate(self.continue_path)
        self.assertEqual(value["chapter"]["content"], self.original + self.raw)
        self.assert_metadata(value, self.original + self.raw, rules=["foreshadowing", "known_info", "timeline"])
        revisions = self.rows()["chapter_revision"]
        self.assertEqual(len(revisions), 1)
        self.assertEqual((revisions[0]["content"], revisions[0]["source"]), (self.original, "continue"))

    def test_native_rewrite_saved_checks_complete_candidate_and_keeps_revision(self):
        value = self.generate(self.rewrite, passage="原文段落", intent="")
        candidate = self.original.replace("原文段落", self.raw, 1)
        self.assertEqual(value["chapter"]["content"], candidate)
        self.assert_metadata(value, candidate, rules=["foreshadowing", "known_info", "timeline"])
        self.assertEqual([row["content"] for row in self.rows()["chapter_revision"]], [self.original])

    def test_native_preview_hashes_candidate_instead_of_old_chapter_and_writes_zero(self):
        before = self.rows()
        value = self.generate(self.rewrite, passage="原文段落", preview=True)
        self.assertEqual(value["chapter"]["content"], self.original)
        candidate = self.original.replace("原文段落", self.raw, 1)
        self.assertEqual(value["content"], candidate)
        self.assert_metadata(value, candidate, subject="preview_candidate", rules=["foreshadowing", "known_info", "timeline"])
        self.assertEqual(self.rows(), before)

    def test_completed_no_hit_explains_literal_rule_limit_and_ignores_foreign_fact(self):
        self.raw = "😀原文与其他未检验的语义。"
        value = self.generate(self.ordinary)
        result = self.assert_metadata(value, self.raw, rules=[])
        self.assertTrue(any("语义" in item and "不" in item for item in result["warnings"]))

    def install_action(self, *, content=None):
        with self.sessions() as db:
            if content is not None:
                db.execute(models.Chapter.__table__.update().where(models.Chapter.id == "prior").values(content=content))
            db.add(models.ChatAction(message_id="source-a", novel_id="work", action_type="chapter", status="done",
                result_id="prior", result_summary={"chapter_id": "prior"}))
            db.commit()

    def test_two_early_existing_action_entries_are_skipped_current_content_zero_generation(self):
        self.install_action(content=self.raw)
        before = self.rows()
        with patch.object(writing.llm, "stream_complete", side_effect=AssertionError("不得生成")):
            native = self.done(self.request(self.chat))
            with self.sessions() as db:
                direct_values = frames("".join(writing.write_chapter(db, "work", action_message_id="source-a")))
        for value in (native, self.done(direct_values)):
            self.assert_metadata(value, self.raw, subject="existing_action", status="skipped", rules=[])
        self.assertEqual(self.rows(), before)

    def test_late_real_chat_action_replay_checks_returned_content_not_new_tokens(self):
        returned = "🐾林越知道青云遗孤，结果在起因之前。谜底。"
        def provider(*_args, **_kwargs):
            self.provider_calls.append({"chunks": ["无规则的新候选😀"]})
            yield "无规则的新候选😀"
            with self.sessions() as db:
                db.add(models.Chapter(id="winner", novel_id="work", seq=2, title="先完成", content=returned))
                db.add(models.ChatAction(message_id="source-a", novel_id="work", action_type="chapter", status="done", result_id="winner", result_summary={}))
                db.commit()
        with patch.object(writing.llm, "stream_complete", side_effect=provider):
            value = self.done(self.request(self.chat))
        self.assertEqual(value["chapter"]["id"], "winner")
        self.assertEqual(value["chapter"]["content"], returned)
        self.assert_metadata(value, returned, rules=["foreshadowing", "known_info", "timeline"])
        self.assertEqual(len(self.rows()["chat_action"]), 1)
        self.assertEqual(len(self.rows()["chapter"]), 2)

    def test_post_model_facts_use_three_fresh_database_rows_not_context_identity_cache(self):
        fresh = "😀新谜。林越知道新秘密。新结果之后新起因。"
        def provider(*_args, **_kwargs):
            yield fresh
            with self.sessions() as other:
                other.execute(models.Foreshadowing.__table__.update().where(models.Foreshadowing.id == "clue").values(content="新谜", status="suspended"))
                other.execute(models.Character.__table__.update().where(models.Character.id == "person").values(known_info="不知道新秘密。"))
                other.execute(models.TimelineEvent.__table__.update().where(models.TimelineEvent.id == "cause").values(title="新起因"))
                other.execute(models.TimelineEvent.__table__.update().where(models.TimelineEvent.id == "effect").values(title="新结果"))
                other.commit()
        with self.sessions() as db:
            cached = [db.get(model, ident) for model, ident in ((models.Foreshadowing, "clue"), (models.Character, "person"), (models.TimelineEvent, "cause"))]
            with patch.object(writing.llm, "stream_complete", side_effect=provider):
                value = self.done(frames("".join(writing.write_chapter(db, "work"))))
        result = self.assert_metadata(value, fresh, rules=["foreshadowing", "known_info", "timeline"])
        self.assertEqual([fresh[row["start"]:row["end"]] for row in result["conflicts"]], ["新谜", "新秘密", "新结果"])
        self.assertEqual(len(cached), 3)

    def test_rule_fact_capture_core_no_autoflush_expire_or_caller_mutation(self):
        from app.application import generated_consistency
        before, sql = self.rows(), []
        with self.sessions() as db:
            clue = db.get(models.Foreshadowing, "clue")
            clue.content, clue.status = "未提交作者稿", "planted"
            pending = models.Character(id="unsaved", novel_id="work", name="未保存人物")
            db.add(pending)
            def trace(_conn, _cursor, statement, *_args):
                sql.append(statement)
            event.listen(self.engine, "before_cursor_execute", trace)
            try:
                with patch.object(db, "expire_all", side_effect=AssertionError("不得expire")), patch.object(db, "flush", side_effect=AssertionError("不得flush")):
                    snapshot = generated_consistency.capture_rule_facts(db, "work")
                result = generated_consistency.result(snapshot, "work", "prior", self.raw, "saved_chapter")
                self.assertEqual([row["rule"] for row in result["conflicts"]], ["foreshadowing", "known_info", "timeline"])
                self.assertIn(clue, db.dirty)
                self.assertIn(pending, db.new)
                self.assertEqual(clue.content, "未提交作者稿")
            finally:
                event.remove(self.engine, "before_cursor_execute", trace)
        self.assertEqual(len(sql), 3)
        self.assertTrue(all(statement.lstrip().startswith("SELECT") for statement in sql))
        self.assertTrue(all(".novel_id = ?" in statement for statement in sql))
        self.assertFalse(any("extras" in statement or "personality" in statement for statement in sql))
        self.assertEqual(self.rows(), before)

    def test_projection_failure_still_saves_and_never_exposes_raw_sql(self):
        def fail(_conn, _cursor, statement, *_args):
            if statement.startswith("SELECT foreshadowing.id, foreshadowing.status, foreshadowing.content"):
                raise RuntimeError("SELECT secret raw author SQL controlled failure")
        event.listen(self.engine, "before_cursor_execute", fail)
        try:
            value = self.generate(self.ordinary)
        finally:
            event.remove(self.engine, "before_cursor_execute", fail)
        result = self.assert_metadata(value, self.raw, status="failed", rules=[])
        self.assertNotIn("SELECT", json.dumps(result, ensure_ascii=False))
        self.assertEqual([row["content"] for row in self.rows()["chapter"] if row["id"] == value["chapter"]["id"]], [self.raw])

    def test_rule_computation_failure_preserves_real_saved_continue_and_revision(self):
        with patch.object(consistency, "rule_conflicts", side_effect=RuntimeError("secret original SQL parameters")):
            value = self.generate(self.continue_path)
        result = self.assert_metadata(value, self.original + self.raw, status="failed", rules=[])
        self.assertNotIn("parameters", json.dumps(result, ensure_ascii=False))
        self.assertEqual([row["content"] for row in self.rows()["chapter_revision"]], [self.original])
        self.assertEqual(next(row["content"] for row in self.rows()["chapter"] if row["id"] == "prior"), self.original + self.raw)

    def test_preview_computation_failure_remains_complete_candidate_zero_write(self):
        before = self.rows()
        with patch.object(consistency, "rule_conflicts", side_effect=ValueError("secret")):
            value = self.generate(self.rewrite, passage="原文段落", preview=True)
        self.assert_metadata(value, value["content"], subject="preview_candidate", status="failed", rules=[])
        self.assertEqual(self.rows(), before)

    def test_native_all_saved_paths_actual_single_commit_then_exclusive_zero_sql(self):
        for path, payload in ((self.ordinary, {}), (self.chat, {}), (self.continue_path, {}), (self.rewrite, {"passage": "原文段落"})):
            with self.subTest(path=path):
                original_commit = self.sessions.class_.commit
                statements, blockers, marker, commits = [], [], {}, []
                def capture(_conn, _cursor, statement, *_args):
                    statements.append(statement)
                def commit(db):
                    original_commit(db)
                    commits.append(1)
                    blocker = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
                    blocker.execute("BEGIN EXCLUSIVE")
                    blockers.append(blocker)
                    marker["after"] = len(statements)
                event.listen(self.engine, "before_cursor_execute", capture)
                try:
                    with patch.object(self.sessions.class_, "commit", new=commit):
                        value = self.generate(path, **payload)
                    self.assertEqual(len(commits), 1)
                    self.assert_metadata(value, value["chapter"]["content"], rules=["foreshadowing", "known_info", "timeline"])
                    self.assertEqual(statements[marker["after"]:], [])
                    self.evidence.setdefault("real_postcommit_EXCLUSIVE", []).append({"path": path, "actual_commits": 1, "postcommit_SQL": 0})
                finally:
                    event.remove(self.engine, "before_cursor_execute", capture)
                    for blocker in blockers:
                        blocker.rollback()
                        blocker.close()

    def test_manual_and_generated_use_same_three_literal_rules_and_original_conflict_shape(self):
        from app.application import generated_consistency
        with self.sessions() as db:
            manual = consistency.consistency_check(db, "work", self.raw)
            snapshot = generated_consistency.capture_rule_facts(db, "work")
        result = generated_consistency.result(snapshot, "work", "prior", self.raw, "saved_chapter")
        self.assertEqual(result["conflicts"], manual["conflicts"])
        self.assertEqual([row["rule"] for row in result["conflicts"]], ["foreshadowing", "known_info", "timeline"])

    def test_real_prior_source_change_still_errors_without_false_saved_or_done_metadata(self):
        def provider(*_args, **_kwargs):
            yield self.raw
            with self.sessions() as other:
                other.execute(models.Chapter.__table__.update().where(models.Chapter.id == "prior").values(content="另一处作者原稿"))
                other.commit()
        with patch.object(writing.llm, "stream_complete", side_effect=provider):
            values = self.request(self.ordinary, previous_chapter_id="prior")
        self.assertEqual(values[-1]["type"], "error")
        self.assertEqual(values[-1]["saved"], False)
        self.assertNotIn("done", [row["type"] for row in values])
        self.assertEqual(len(self.rows()["chapter"]), 1)


if __name__ == "__main__":
    unittest.main()
