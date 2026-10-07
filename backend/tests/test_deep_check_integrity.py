"""深度检查真实HTTP：统计守恒、结构失败、同作品引用事实及只读事务。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from app.application import consistency as check
from app.infrastructure import models, repository as repos
from app.infrastructure.db import get_db
from app.interfaces.api.v1 import consistency


class DeepCheckIntegrityTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="lingmo-deep-check-")
        self.addCleanup(directory.cleanup)
        self.engine = create_engine(f"sqlite:///{(Path(directory.name) / 'review.db').as_posix()}",
                                   connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        models.Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        self.nid = repos.NovelRepo(self.db).create(title="深度审校").id
        self.other = repos.NovelRepo(self.db).create(title="另一作品").id
        self.lin = repos.CharacterRepo(self.db).create(self.nid, name="林舟").id
        self.endpoint = f"/api/v1/novels/{self.nid}/consistency/deep"
        app = FastAPI()
        app.include_router(consistency.router, prefix="/api/v1")

        def isolated_db():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = isolated_db
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        for target in ("httpx.HTTPTransport.handle_request", "httpx.AsyncHTTPTransport.handle_async_request",
                       "app.infrastructure.ai.embedding.EmbeddingService.embed"):
            guard = patch(target, side_effect=AssertionError("深度检查不能调用HTTP或嵌入服务"))
            guard.start(); self.addCleanup(guard.stop)

    def request(self, content="林舟登上灯塔。", *, raw='{"conflicts":[]}', outputs=None):
        options = {"side_effect": outputs} if outputs is not None else {"return_value": raw}
        with patch.object(check.llm, "complete", **options) as mocked:
            response = self.client.post(self.endpoint, json={"content": content})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assert_counts(result, mocked.call_count)
        return result, mocked

    def assert_counts(self, result, calls):
        self.assertEqual(result["chunks_total"], result["chunks_eligible"] + result["chunks_skipped"])
        self.assertEqual(result["chunks_skipped"], result["chunks_skipped_rule"] + result["chunks_skipped_no_entity"])
        self.assertEqual(result["chunks_eligible"], result["chunks_attempted"] + result["chunks_unchecked"])
        self.assertEqual(result["chunks_attempted"], result["chunks_checked"] + result["chunks_failed"])
        self.assertEqual(result["chunks_attempted"], calls)
        self.assertLessEqual(calls, check.MAX_CHUNKS)
        self.assertEqual(result["truncated"], result["chunks_unchecked"] > 0)
        expected = ("skipped" if not calls else "failed" if not result["chunks_checked"] else
                    "partial" if result["chunks_failed"] or result["chunks_unchecked"] else "complete")
        self.assertEqual(result["status"], expected)
        self.assertIsInstance(result["warnings"], list)

    @staticmethod
    def valid(excerpt="林舟登上灯塔", **extra):
        return {"type": "人设矛盾", "excerpt": excerpt, "reason": "设定中人物恐高", "severity": "high", **extra}

    @staticmethod
    def prompt_context(mocked, call=0):
        prompt = mocked.call_args_list[call].args[0][1]["content"]
        return json.loads(prompt.split("设定：\n", 1)[1].split("\n\n正文片段：", 1)[0])

    def test_complete_counts_valid_conflicts_and_empty_array_are_successes(self):
        result, _ = self.request(raw=json.dumps({"conflicts": [self.valid()]}))
        self.assertEqual((result["status"], result["chunks_checked"], len(result["conflicts"])), ("complete", 1, 1))
        self.assertEqual(result["conflicts"][0]["type"], "人设矛盾")
        self.assertEqual(result["warnings"], [])
        result, _ = self.request()
        self.assertEqual((result["status"], result["chunks_checked"], result["conflicts"]), ("complete", 1, []))

    def test_all_model_failures_are_failed_not_completed(self):
        result, _ = self.request("林舟登上灯塔。\n\n林舟走下阶梯。",
                                 outputs=[RuntimeError("模型不可用"), RuntimeError("模型不可用")])
        self.assertEqual((result["status"], result["chunks_checked"], result["chunks_failed"]), ("failed", 0, 2))
        self.assertEqual(result["conflicts"], [])
        self.assertTrue(all("未完成" in warning for warning in result["warnings"]))

    def test_partial_failure_keeps_other_chunk_result_and_original_index(self):
        result, _ = self.request("林舟登上灯塔。\n\n林舟走下阶梯。", outputs=[RuntimeError("首块故障"),
                                 json.dumps({"conflicts": [self.valid("林舟走下阶梯")]})])
        self.assertEqual((result["status"], result["chunks_checked"], result["chunks_failed"]), ("partial", 1, 1))
        self.assertEqual(result["conflicts"][0]["chunk_index"], 1)
        self.assertIn("第 1 块", result["warnings"][0])

    def test_empty_or_nameless_text_is_skipped_without_model(self):
        # HTTP仍保持旧min_length=1约束；用例本身也明确处理没有分块的空输入。
        self.assertEqual(self.client.post(self.endpoint, json={"content": ""}).status_code, 422)
        with patch.object(check.llm, "complete") as mocked:
            result = check.deep_check(self.db, self.nid, "")
        self.assert_counts(result, mocked.call_count)
        self.assertEqual(result["status"], "skipped")
        for content, total in ((" \n\t\n", 0), ("只有海风。\n\n依然没有设定名称。", 2)):
            with self.subTest(content=content):
                result, _ = self.request(content)
                self.assertEqual((result["status"], result["chunks_total"], result["chunks_attempted"]), ("skipped", total, 0))

    def test_rule_only_skip_returns_original_rules_and_never_calls_model(self):
        clue = repos.ForeshadowingRepo(self.db).create(self.nid, content="身世之谜", status="consumed")
        result, _ = self.request("林舟再次追寻身世之谜。")
        self.assertEqual((result["status"], result["chunks_skipped_rule"], result["chunks_skipped_no_entity"]), ("skipped", 1, 0))
        self.assertEqual(result["rule_conflicts"][0]["entity_id"], clue.id)
        self.assertEqual(result["conflicts"], [])

    def test_rule_skip_and_no_entity_skip_are_disjoint_with_complete_eligible_scope(self):
        repos.ForeshadowingRepo(self.db).create(self.nid, content="身世之谜", status="consumed")
        result, _ = self.request("林舟追寻身世之谜。\n\n只有海风。\n\n林舟登上灯塔。")
        self.assertEqual((result["status"], result["chunks_skipped_rule"], result["chunks_skipped_no_entity"],
                          result["chunks_checked"]), ("complete", 1, 1, 1))
        self.assertEqual(len(result["warnings"]), 2)

    def test_eight_block_limit_reports_tail_and_all_failure_remains_failed(self):
        content = "\n\n".join(f"林舟在第{index}个场景停步。" for index in range(10))
        result, _ = self.request(content)
        self.assertEqual((result["status"], result["chunks_checked"], result["chunks_unchecked"]), ("partial", 8, 2))
        self.assertTrue(any("另有 2 块" in warning for warning in result["warnings"]))
        result, _ = self.request(content, outputs=[RuntimeError("offline")] * 8)
        self.assertEqual((result["status"], result["chunks_failed"], result["chunks_unchecked"]), ("failed", 8, 2))

    def test_malformed_root_and_conflicts_shapes_fail_safely_without_500(self):
        for value in (None, [], True, 1, "text", {}, {"conflicts": None}, {"conflicts": {}}, {"conflicts": "wrong"},
                      {"conflicts": [None]}, {"conflicts": [7]}, {"conflicts": ["wrong"]}):
            with self.subTest(value=value):
                result, _ = self.request(raw=json.dumps(value))
                self.assertEqual((result["status"], result["chunks_checked"], result["chunks_failed"]), ("failed", 0, 1))
                self.assertEqual(result["conflicts"], [])
        result, _ = self.request(raw="not JSON")
        self.assertEqual(result["status"], "failed")

    def test_bad_item_invalidates_whole_chunk_without_leaking_earlier_valid_item(self):
        bad_items = [{}, self.valid(type=""), self.valid(type=False), self.valid(reason=" \n"),
                     self.valid(reason=7), self.valid(excerpt=""), self.valid(excerpt="原文不存在"),
                     self.valid(severity="unknown"), self.valid(severity=None)]
        for item in bad_items:
            with self.subTest(item=item):
                result, _ = self.request(raw=json.dumps({"conflicts": [self.valid(), item]}))
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["conflicts"], [])

    def test_excerpt_in_another_chunk_cannot_make_current_chunk_checked(self):
        content = "林舟登上灯塔。\n\n林舟走下阶梯。"
        result, _ = self.request(content, outputs=[json.dumps({"conflicts": [self.valid("林舟走下阶梯")]}),
                                                   '{"conflicts":[]}'])
        self.assertEqual((result["status"], result["chunks_failed"], result["chunks_checked"]), ("partial", 1, 1))
        self.assertEqual(result["conflicts"], [])

    def test_name_with_brackets_multiline_known_info_extras_and_duplicate_names_are_preserved(self):
        repos.CharacterRepo(self.db).update(self.nid, self.lin, name="林[舟]", known_info="第一行秘密。\n第二行限制。",
                                             extras={"左手": "不能负重"})
        another = repos.CharacterRepo(self.db).create(self.nid, name="林[舟]", role="另一个同名人物").id
        result, mocked = self.request("林[舟]把箱子抬上了楼。")
        facts = self.prompt_context(mocked)["characters"]
        self.assertEqual({row["id"] for row in facts}, {self.lin, another})
        actual = next(row for row in facts if row["id"] == self.lin)
        self.assertEqual(actual["known_info"], "第一行秘密。\n第二行限制。")
        self.assertEqual(actual["extras"], {"左手": "不能负重"})
        self.assertEqual(result["status"], "complete")

    def test_one_relationship_endpoint_adds_needed_counterpart_but_not_unrelated_facts(self):
        mother = repos.CharacterRepo(self.db).create(self.nid, name="沈瑶", role="林舟的母亲", known_info="母亲掌握的事实").id
        unrelated = repos.CharacterRepo(self.db).create(self.nid, name="无关人物", known_info="无关秘密").id
        relevant = repos.RelationshipRepo(self.db).create(self.nid, subject_id=self.lin, object_id=mother,
                                                          relation_type="母子", note="沈瑶是林舟的生母").id
        repos.RelationshipRepo(self.db).create(self.nid, subject_id=mother, object_id=unrelated, relation_type="旧识")
        _, mocked = self.request("林舟说他与沈姓女子毫无亲属关系。")
        facts = self.prompt_context(mocked)
        self.assertEqual({row["id"] for row in facts["characters"]}, {self.lin, mother})
        self.assertEqual([row["id"] for row in facts["relationships"]], [relevant])
        self.assertEqual(facts["relationships"][0]["note"], "沈瑶是林舟的生母")
        self.assertNotIn("无关秘密", json.dumps(facts, ensure_ascii=False))

    def test_related_foreshadow_events_and_explicit_cause_chain_are_in_context(self):
        cause = repos.TimelineEventRepo(self.db).create(self.nid, title="灯塔失火", description="前序因果事实").id
        event = repos.TimelineEventRepo(self.db).create(self.nid, title="铜牌移交", participants=[self.lin], cause_id=cause,
                                                       description="移交给林舟").id
        clue = repos.ForeshadowingRepo(self.db).create(self.nid, content="铜牌刻有三道划痕", related_characters=[self.lin]).id
        _, mocked = self.request("林舟掌中的铜牌光滑无纹。")
        facts = self.prompt_context(mocked)
        self.assertEqual({row["id"] for row in facts["events"]}, {cause, event})
        self.assertEqual([row["id"] for row in facts["foreshadowings"]], [clue])
        self.assertEqual(facts["foreshadowings"][0]["content"], "铜牌刻有三道划痕")

    def test_place_parent_and_world_fields_are_fact_dependencies(self):
        parent = repos.LocationRepo(self.db).create(self.nid, name="旧城区", description="城区事实").id
        child = repos.LocationRepo(self.db).create(self.nid, name="灯塔", parent_id=parent, description="塔内无电").id
        world = repos.WorldSettingRepo(self.db).create(self.nid, name="修炼禁制", description="只能夜间施法", tags=["法则"]).id
        _, mocked = self.request("灯塔里的修炼禁制已经失效。")
        facts = self.prompt_context(mocked)
        self.assertEqual({row["id"] for row in facts["locations"]}, {parent, child})
        self.assertEqual([row["id"] for row in facts["world_settings"]], [world])

    def test_foreign_missing_and_ambiguous_old_references_never_borrow_other_novel_facts(self):
        foreign = repos.CharacterRepo(self.db).create(self.other, name="异作人物", known_info="不可泄漏异作秘密").id
        foreign_chapter = repos.ChapterRepo(self.db).create(self.other, content="异作正文").id
        bad_relationships = [repos.RelationshipRepo(self.db).create(self.nid, subject_id=self.lin,
                                    object_id=endpoint, relation_type="旧坏引用").id for endpoint in (foreign, "missing")]
        repos.ForeshadowingRepo(self.db).create(self.nid, content="旧坏引用伏笔", related_characters=[self.lin, foreign],
                                              planted_chapter_id=foreign_chapter)
        _, mocked = self.request("林舟查看旧坏引用伏笔。")
        facts = self.prompt_context(mocked)
        raw = json.dumps(facts, ensure_ascii=False)
        self.assertEqual(facts["relationships"], [])
        self.assertEqual(facts["foreshadowings"][0]["related_characters"], [self.lin])
        self.assertEqual(facts["foreshadowings"][0]["planted_chapter_id"], "")
        for token in (foreign, foreign_chapter, "不可泄漏异作秘密", "异作正文", *bad_relationships):
            self.assertNotIn(token, raw)

    def test_event_only_and_foreshadow_only_blocks_do_not_add_model_calls(self):
        repos.TimelineEventRepo(self.db).create(self.nid, title="灯塔失火")
        repos.ForeshadowingRepo(self.db).create(self.nid, content="铜牌刻痕")
        result, _ = self.request("灯塔失火。\n\n铜牌刻痕仍有谜团。")
        self.assertEqual((result["status"], result["chunks_skipped_no_entity"], result["chunks_attempted"]), ("skipped", 2, 0))

    def test_unknown_or_deleted_novel_is_404_even_for_empty_or_nameless_content(self):
        with patch.object(check.llm, "complete") as model:
            for nid in ("missing", self.other):
                if nid == self.other:
                    repos.NovelRepo(self.db).delete(nid)
                response = self.client.post(f"/api/v1/novels/{nid}/consistency/deep", json={"content": " "})
                self.assertEqual(response.status_code, 404)
            model.assert_not_called()

    def test_no_write_commit_or_rollback_and_caller_flushed_core_transactions_are_retained(self):
        for mode in ("flushed", "core"):
            with self.subTest(mode=mode), self.Session() as db:
                if mode == "flushed":
                    db.add(models.Character(novel_id=self.nid, name="调用方人物")); db.flush()
                else:
                    db.execute(text("UPDATE novel SET summary='调用方摘要' WHERE id=:id"), {"id": self.nid})
                statements = []
                listener = lambda _conn, _cursor, sql, *_: statements.append(sql)
                event.listen(self.engine, "before_cursor_execute", listener)
                try:
                    with patch.object(db, "commit", wraps=db.commit) as commit, \
                         patch.object(db, "rollback", wraps=db.rollback) as rollback, \
                         patch.object(check.llm, "complete", return_value='{"conflicts":[]}'):
                        result = check.deep_check(db, self.nid, "林舟登上灯塔。")
                    commit.assert_not_called(); rollback.assert_not_called()
                    self.assertTrue(all(sql.lstrip().upper().startswith("SELECT") for sql in statements), statements)
                    self.assertEqual(result["status"], "complete")
                finally:
                    event.remove(self.engine, "before_cursor_execute", listener)
                db.commit()
        with self.Session() as db:
            self.assertIsNotNone(db.query(models.Character).filter(models.Character.name == "调用方人物").first())
            self.assertEqual(repos.NovelRepo(db).get(self.nid).summary, "调用方摘要")

    def test_deep_http_leaves_all_stored_rows_unchanged_with_success_or_model_failure(self):
        with self.Session() as db:
            before = {table.name: [dict(row) for row in db.execute(select(table)).mappings()]
                      for table in models.Base.metadata.sorted_tables}
        self.request()
        self.request(outputs=[RuntimeError("controlled failure")])
        with self.Session() as db:
            after = {table.name: [dict(row) for row in db.execute(select(table)).mappings()]
                     for table in models.Base.metadata.sorted_tables}
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
