"""关系详情的真实 HTTP、作品归属与实体/索引同事务合同。"""
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.application import setting_index
from app.infrastructure import models
from app.infrastructure.db import get_db
from app.interfaces.api.v1 import relationships


class RelationshipRoutesTest(unittest.TestCase):
    fields = {"id", "novel_id", "subject_id", "object_id", "relation_type", "note", "created_at", "updated_at"}
    raw = "\r\n　作者精确关系稿😀\0　\n"

    def setUp(self):
        temporary = TemporaryDirectory(prefix="lingmo-relationship-routes-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "关系详情真事务.sqlite"
        self.engine = create_engine(f"sqlite:///{self.path.as_posix()}",
            connect_args={"check_same_thread": False, "timeout": .03})
        self.addCleanup(self.engine.dispose)
        models.Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=True)
        self.nodes = []
        with self.sessions() as db:
            db.add_all([models.Novel(id="work", title="作品甲", style_anchor="已有文风😀\0"),
                models.Novel(id="foreign", title="作品乙", style_anchor="不改文风")])
            db.flush()
            for novel_id in ("work", "foreign"):
                for index, (model, label) in enumerate(((models.Character, "name"), (models.Location, "name"),
                    (models.WorldSetting, "name"), (models.TimelineEvent, "title"), (models.Foreshadowing, "content"))):
                    item_id, name = f"{novel_id}-node-{index}", f"{novel_id}第{index}类节点😀"
                    data = {"id": item_id, "novel_id": novel_id, label: name}
                    if model is models.Character:
                        data["extras"] = {"乙": [None, False, 0], "甲": {"保留": "😀\0"}}
                    elif model is models.WorldSetting:
                        data["tags"] = ["乙", "甲"]
                    db.add(model(**data))
                    if novel_id == "work":
                        self.nodes.append((item_id, name))
                db.add(models.Chapter(id=f"{novel_id}-chapter", novel_id=novel_id, seq=1,
                    title="已有章", content="原正文😀\0", directive="原指令"))
                db.add(models.MemoryChunk(id=f"{novel_id}-slice", novel_id=novel_id, kind="chapter_slice",
                    source_chapter_id=f"{novel_id}-chapter", embedding_id="保留切片", text="原章切片😀\0", vector=[0., 1.]))
            db.add(models.Conversation(id="thread", novel_id="work", title="原会话"))
            db.add(models.Message(id="message", conversation_id="thread", role="assistant",
                content="原消息😀\0", meta={"原": [None, True]}))
            db.commit()
        self.embed_calls, self.trace, self.blockers = [], [], []
        self.lock_after_commit = False
        embedding_patch = patch.object(setting_index.embedding, "embed", side_effect=self.controlled_embedding)
        self.embedding = embedding_patch.start()
        self.addCleanup(embedding_patch.stop)
        self.app = FastAPI()
        self.app.include_router(relationships.router, prefix="/api/v1")

        def dependency():
            with self.sessions() as db:
                yield db

        self.app.dependency_overrides[get_db] = dependency
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.meter = {"active": False, "commits": 0, "post_sql": [], "writes": []}
        original_commit = self.sessions.class_.commit

        def commit(db):
            original_commit(db)
            if self.meter["active"] and db.bind is self.engine:
                self.meter["commits"] += 1
                if self.lock_after_commit:
                    blocker = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
                    blocker.execute("BEGIN EXCLUSIVE")
                    self.blockers.append(blocker)

        commit_patch = patch.object(self.sessions.class_, "commit", new=commit)
        commit_patch.start()
        self.addCleanup(commit_patch.stop)

        def capture(_connection, _cursor, statement, *_args):
            if self.meter["active"]:
                if self.meter["commits"]:
                    self.meter["post_sql"].append(statement)
                if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
                    self.meter["writes"].append(statement)

        event.listen(self.engine, "before_cursor_execute", capture)
        self.addCleanup(event.remove, self.engine, "before_cursor_execute", capture)
        self.original = self.request("POST", "/api/v1/novels/work/relationships", {
            "subject_id": self.nodes[0][0], "object_id": self.nodes[1][0], "relation_type": "原关系", "note": self.raw}, 201).json()
        self.foreign = self.request("POST", "/api/v1/novels/foreign/relationships", {
            "subject_id": "foreign-node-0", "object_id": "foreign-node-1", "relation_type": "乙关系", "note": "不改乙稿"}, 201).json()
        self.url = f"/api/v1/novels/work/relationships/{self.original['id']}"

    def controlled_embedding(self, texts):
        self.embed_calls.append({"texts": list(texts), "mode": "controlled_fixed_vectors"})
        return [[1., 0.] for _ in texts]

    def tearDown(self):
        self.release_locks()
        self.evidence = {"native_http": self.trace, "controlled_embedding_calls": self.embed_calls,
            "final_tables": self.rows(), "fixture_database": str(self.path)}

    def release_locks(self):
        self.lock_after_commit = False
        for blocker in self.blockers:
            blocker.rollback()
            blocker.close()
        self.blockers.clear()

    def rows(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            return {name: [dict(row) for row in db.execute(f'SELECT * FROM "{name}" ORDER BY rowid')] for name in names}

    def request(self, method, path, payload=None, expected=200):
        self.meter.update(active=True, commits=0, post_sql=[], writes=[])
        try:
            response = self.client.request(method, path, **({"json": payload} if payload is not None else {}))
            try:
                body = response.json()
            except ValueError:
                body = response.text
            self.trace.append({"method": method, "path": path, "payload": payload, "http": response.status_code,
                "body": body, "commits": self.meter["commits"], "postcommit_response_SQL": self.meter["post_sql"].copy(),
                "write_SQL": self.meter["writes"].copy(), "actual_postcommit_exclusive_held": bool(self.blockers)})
            self.assertEqual(response.status_code, expected, response.text)
            if method == "GET" or expected >= 400:
                self.assertEqual(self.meter["commits"], 0)
            else:
                self.assertEqual(self.meter["commits"], 1)
                self.assertEqual(self.meter["post_sql"], [])
            return response
        finally:
            self.meter["active"] = False

    def update(self, payload, expected=200):
        return self.request("PUT", self.url, payload, expected)

    def assert_actual_response(self, body):
        self.assertEqual(set(body), self.fields)
        self.assertEqual(body, next(row for row in self.rows()["relationship"] if row["id"] == self.original["id"]))

    def assert_unrelated_unchanged(self, before):
        after = self.rows()
        for table in before:
            def unrelated(rows):
                if table == "relationship":
                    return [row for row in rows if row["id"] != self.original["id"]]
                if table == "memory_chunk":
                    return [row for row in rows if not (row["novel_id"] == "work" and row["kind"] == "setting"
                        and row["embedding_id"] == self.original["id"])]
                return rows
            self.assertEqual(unrelated(after[table]), unrelated(before[table]), table)

    def own_index(self):
        return [row for row in self.rows()["memory_chunk"] if row["novel_id"] == "work"
            and row["kind"] == "setting" and row["embedding_id"] == self.original["id"]]

    def test_native_get_eight_exact_fields_and_scoped_reads_are_read_only(self):
        before, embeds = self.rows(), len(self.embed_calls)
        self.assertEqual(len(before), 21)
        self.assertIn('outline_append_receipt', before)
        self.assertEqual(self.request("GET", self.url).json(), self.original)
        self.assert_actual_response(self.original)
        for path in (f"/api/v1/novels/foreign/relationships/{self.original['id']}",
            "/api/v1/novels/work/relationships/missing", "/api/v1/novels/missing/relationships/missing"):
            self.request("GET", path, expected=404)
        self.assertEqual(self.rows(), before)
        self.assertEqual(len(self.embed_calls), embeds)
        self.assertTrue(all(not row["write_SQL"] for row in self.trace if row["method"] == "GET"))

    def test_native_partial_note_preserves_other_fields_and_returns_actual_row(self):
        before = self.rows()
        changed = "\n　只改 note 的作者稿🐈\0　\r\n"
        body = self.update({"note": changed}).json()
        self.assert_actual_response(body)
        self.assertEqual(body["note"], changed)
        for field in self.fields - {"note", "updated_at"}:
            self.assertEqual(body[field], self.original[field], field)
        self.assert_unrelated_unchanged(before)

    def test_native_null_ignored_and_explicit_empty_strings_clear_eligible_fields(self):
        current = self.update({"relation_type": "新关系😀", "note": self.raw}).json()
        ignored = self.update({"subject_id": None, "object_id": None, "relation_type": None, "note": None}).json()
        for field in self.fields - {"updated_at"}:
            self.assertEqual(ignored[field], current[field], field)
        cleared = self.update({"note": "", "relation_type": ""}).json()
        self.assertEqual((cleared["note"], cleared["relation_type"]), ("", ""))
        self.assertEqual((cleared["subject_id"], cleared["object_id"]), (current["subject_id"], current["object_id"]))
        self.assert_actual_response(cleared)

    def test_native_endpoint_updates_accept_all_twenty_five_same_novel_node_pairs(self):
        before = self.rows()
        for subject, subject_name in self.nodes:
            for object_id, object_name in self.nodes:
                with self.subTest(subject=subject, object_id=object_id):
                    body = self.update({"subject_id": subject, "object_id": object_id}).json()
                    self.assert_actual_response(body)
                    self.assertEqual((body["subject_id"], body["object_id"]), (subject, object_id))
                    chunk = self.own_index()
                    self.assertEqual(len(chunk), 1)
                    self.assertIn(subject_name, chunk[0]["text"])
                    self.assertIn(object_name, chunk[0]["text"])
        self.assert_unrelated_unchanged(before)

    def test_native_invalid_endpoints_roll_back_entity_and_index_in_all_twenty_tables(self):
        before, embeds = self.rows(), len(self.embed_calls)
        invalid = ["foreign-node-0", "missing", "", " \t\n", "work-chapter", self.original["id"]]
        for field in ("subject_id", "object_id"):
            for value in invalid:
                with self.subTest(field=field, value=value):
                    self.update({field: value, "note": "不得保留失败新稿"}, 400)
                    self.assertEqual(self.rows(), before)
        self.assertEqual(len(self.embed_calls), embeds)

    def test_native_foreign_and_missing_targets_return404_without_mutations(self):
        before, embeds = self.rows(), len(self.embed_calls)
        for path in (f"/api/v1/novels/work/relationships/{self.foreign['id']}",
            f"/api/v1/novels/foreign/relationships/{self.original['id']}",
            "/api/v1/novels/work/relationships/missing", f"/api/v1/novels/missing/relationships/{self.original['id']}"):
            for method, payload in (("GET", None), ("PUT", {"note": "外作品不得修改"}), ("DELETE", None)):
                self.request(method, path, payload, 404)
        self.assertEqual(self.rows(), before)
        self.assertEqual(len(self.embed_calls), embeds)
        self.assertTrue(all(not row["write_SQL"] for row in self.trace[2:]))

    def test_native_note_only_edit_keeps_unrelated_historical_bad_endpoint(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE relationship SET subject_id='historical-missing' WHERE id=?", (self.original["id"],))
        before = self.rows()
        body = self.update({"note": "历史关系可修说明😀\0"}).json()
        self.assertEqual(body["subject_id"], "historical-missing")
        self.assert_actual_response(body)
        self.assertIn("人物已删除", self.own_index()[0]["text"])
        self.assert_unrelated_unchanged(before)

    def test_native_update_replaces_own_index_with_current_names_and_author_text(self):
        before, old = self.rows(), self.own_index()[0]
        body = self.update({"subject_id": self.nodes[3][0], "object_id": self.nodes[4][0],
            "relation_type": "新联系😀", "note": self.raw + "新说明"}).json()
        self.assert_actual_response(body)
        chunks = self.own_index()
        self.assertEqual(len(chunks), 1)
        self.assertNotEqual(chunks[0]["id"], old["id"])
        for text in (self.nodes[3][1], self.nodes[4][1], body["relation_type"], body["note"]):
            self.assertIn(text, chunks[0]["text"])
        self.assertNotIn(self.nodes[0][1], chunks[0]["text"])
        self.assert_unrelated_unchanged(before)

    def test_native_real_index_insert_and_delete_failures_roll_back_all_tables(self):
        for operation in ("INSERT", "DELETE"):
            with self.subTest(operation=operation):
                with self.engine.begin() as connection:
                    side = "NEW" if operation == "INSERT" else "OLD"
                    connection.exec_driver_sql(f"CREATE TRIGGER refuse_relationship_index BEFORE {operation} ON memory_chunk "
                        f"WHEN {side}.embedding_id='{self.original['id']}' BEGIN SELECT RAISE(ABORT,'真实关系索引拒绝'); END")
                before = self.rows()
                try:
                    self.update({"note": "不得残留的关系新稿😀\0", "relation_type": "不得残留的新关系"}, 500)
                    self.assertEqual(self.rows(), before)
                    self.assertEqual(len(self.own_index()), 1)
                finally:
                    with self.engine.begin() as connection:
                        connection.exec_driver_sql("DROP TRIGGER refuse_relationship_index")

    def test_native_real_relationship_write_failure_preserves_entity_and_old_index(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql("CREATE TRIGGER refuse_relationship BEFORE UPDATE ON relationship "
                "BEGIN SELECT RAISE(ABORT,'真实关系写入拒绝'); END")
        before, embeds = self.rows(), len(self.embed_calls)
        self.update({"note": "不保存的实体稿"}, 500)
        self.assertEqual(self.rows(), before)
        self.assertEqual(len(self.embed_calls), embeds)

    def test_native_embedding_failure_still_saves_author_edit_and_removes_stale_index(self):
        for kind in ("exception", "wrong_count", "invalid_vector", "nonfinite_vector"):
            with self.subTest(kind=kind):
                self.embedding.side_effect = self.controlled_embedding
                self.update({"note": f"{kind}原索引"})
                self.assertEqual(len(self.own_index()), 1)
                before = self.rows()

                def failure(texts):
                    self.embed_calls.append({"texts": list(texts), "mode": f"controlled_{kind}"})
                    if kind == "exception":
                        raise RuntimeError("受控嵌入不可用")
                    return {"wrong_count": [], "invalid_vector": [[True]], "nonfinite_vector": [[float("nan")]]}[kind]

                self.embedding.side_effect = failure
                body = self.update({"note": f"{kind}作者明确新稿😀\0"}).json()
                self.assert_actual_response(body)
                self.assertEqual(body["note"], f"{kind}作者明确新稿😀\0")
                self.assertEqual(self.own_index(), [])
                self.assert_unrelated_unchanged(before)

    def test_native_postcommit_exclusive_returns_eight_fields_without_response_sql(self):
        before = self.rows()
        self.lock_after_commit = True
        try:
            response = self.update({"note": "提交后真锁仍可确认😀\0", "relation_type": "已保存关系"})
            body = response.json()
            self.assertEqual(set(body), self.fields)
            self.assertEqual(len(self.blockers), 1)
            self.assertTrue(self.trace[-1]["actual_postcommit_exclusive_held"])
            self.assertEqual(self.trace[-1]["postcommit_response_SQL"], [])
            self.assertEqual(body["note"], "提交后真锁仍可确认😀\0")
        finally:
            self.release_locks()
        self.assert_actual_response(body)
        self.assert_unrelated_unchanged(before)

    def test_native_original_create_list_delete_remain_scoped_and_remove_index(self):
        before = self.rows()
        listed = self.request("GET", "/api/v1/novels/work/relationships").json()
        self.assertEqual(listed, [self.original])
        self.request("DELETE", f"/api/v1/novels/foreign/relationships/{self.original['id']}", expected=404)
        self.request("DELETE", self.url, expected=204)
        self.request("GET", self.url, expected=404)
        self.assertEqual(self.request("GET", "/api/v1/novels/work/relationships").json(), [])
        self.assertEqual(self.own_index(), [])
        self.assert_unrelated_unchanged(before)

    def test_native_wrong_json_field_types_return422_without_writes(self):
        before, embeds = self.rows(), len(self.embed_calls)
        for payload in ({"note": {"稿": "不能替代文本"}}, {"note": []}, {"relation_type": 3}, {"subject_id": 5}):
            self.update(payload, 422)
        self.assertEqual(self.rows(), before)
        self.assertEqual(len(self.embed_calls), embeds)
        self.assertTrue(all(not row["write_SQL"] for row in self.trace[2:]))


if __name__ == "__main__":
    unittest.main()
