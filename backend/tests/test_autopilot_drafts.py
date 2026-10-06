"""独立 SQLite 半稿 API：恢复、作品隔离、陈旧写入和删除墓碑。"""
import unittest
import uuid
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.infrastructure import models
from app.infrastructure.autopilot_drafts import AutopilotDraftRepo, DraftStorageError
from app.infrastructure.db import get_db
from app.infrastructure.repository import NovelRepo
from app.interfaces.api.v1.autopilot_drafts import router


class AutopilotDraftTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        models.Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.sessions()
        self.novel_id = NovelRepo(self.db).create(title="作品甲").id
        self.other_id = NovelRepo(self.db).create(title="作品乙").id
        self.draft_id = str(uuid.uuid4())
        self.node_id = str(uuid.uuid4())
        self.payload = {
            "node_id": self.node_id, "title": "生成到一半的第二章", "attempt": 1,
            "content": "汉字半稿\n换行与末尾半句话", "reason": "", "state": "active", "revision": 1,
        }
        isolated_app = FastAPI()
        isolated_app.include_router(router, prefix="/api/v1")

        def override_db():
            db = self.sessions()
            try:
                yield db
            finally:
                db.close()

        isolated_app.dependency_overrides[get_db] = override_db
        self.client = TestClient(isolated_app)
        self.url = f"/api/v1/novels/{self.novel_id}/autopilot/drafts"
        self.other_url = f"/api/v1/novels/{self.other_id}/autopilot/drafts"
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.addCleanup(self.client.close)

    def _put(self, *, draft_id=None, url=None, **changes):
        return self.client.put(
            f"{url or self.url}/{draft_id or self.draft_id}", json={**self.payload, **changes},
        )

    def _rows(self):
        self.db.expire_all()
        return {
            table.name: [dict(row) for row in self.db.execute(select(table)).mappings()]
            for table in models.Base.metadata.sorted_tables
        }

    def test_save_and_reopen_full_unicode_content_and_read_only_recovery(self):
        created = self._put()
        self.assertEqual(created.status_code, 200)
        saved = created.json()
        self.assertEqual(saved["state"], "active")
        self.assertEqual(saved["novel_id"], self.novel_id)
        self.assertEqual(saved["content"], self.payload["content"])
        self.assertTrue(saved["created_at"])
        before = self._rows()
        recovered = self.client.get(self.url).json()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["state"], "unconfirmed")
        self.assertIn("尚未确认", recovered[0]["reason"])
        self.assertEqual(recovered[0]["content"], saved["content"])
        self.assertEqual(self._rows(), before)

    def test_newer_revision_updates_one_row_and_preserves_creation_timestamp(self):
        first = self._put().json()
        updated = self._put(content="完整收到的新片段。", revision=7, state="failed", reason="输出上限").json()
        self.assertEqual(updated["created_at"], first["created_at"])
        self.assertEqual(updated["content"], "完整收到的新片段。")
        self.assertEqual(updated["revision"], 7)
        self.assertEqual(self.client.get(self.url).json(), [updated])
        self.assertEqual(len(self._rows()["autopilot_draft"]), 1)

    def test_same_revision_same_payload_is_idempotent_without_timestamp_change(self):
        first = self._put().json()
        self.assertEqual(self._put().json(), first)
        self.assertEqual(len(self._rows()["autopilot_draft"]), 1)

    def test_older_revision_and_same_revision_conflict_never_overwrite_new_text(self):
        latest = self._put(revision=3, content="已经保存的新内容").json()
        for revision in (1, 3):
            response = self._put(revision=revision, content="迟到的旧内容")
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["code"], "draft_stale")
        saved = self._rows()["autopilot_draft"][0]
        self.assertEqual(saved["content"], latest["content"])
        self.assertEqual(saved["updated_at"], latest["updated_at"])

    def test_draft_cannot_change_node_or_attempt(self):
        self._put()
        for changes in ({"node_id": str(uuid.uuid4())}, {"attempt": 2}):
            response = self._put(revision=2, **changes)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["code"], "draft_identity_conflict")

    def test_novels_have_separate_lists_and_cannot_overwrite_or_delete_each_other(self):
        first = self._put().json()
        other_draft = str(uuid.uuid4())
        self._put(draft_id=other_draft, url=self.other_url, content="乙作品半稿")
        self.assertEqual([row["id"] for row in self.client.get(self.url).json()], [first["id"]])
        self.assertEqual([row["id"] for row in self.client.get(self.other_url).json()], [other_draft])
        response = self._put(url=self.other_url, revision=2)
        self.assertEqual(response.status_code, 404)
        response = self.client.delete(f"{self.other_url}/{self.draft_id}")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(self._rows()["autopilot_draft"]), 2)

    def test_delete_clears_content_and_never_resurrects_from_late_save(self):
        self._put()
        response = self.client.delete(f"{self.url}/{self.draft_id}?revision=9")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"id": self.draft_id, "deleted": True, "revision": 9})
        self.assertEqual(self.client.get(self.url).json(), [])
        row = self._rows()["autopilot_draft"][0]
        self.assertEqual((row["content"], row["reason"], row["title"]), ("", "", ""))
        for revision in (1, 10, 100):
            response = self._put(revision=revision)
            self.assertEqual(response.status_code, 410)
            self.assertEqual(response.json()["code"], "draft_deleted")
        again = self.client.delete(f"{self.url}/{self.draft_id}")
        self.assertEqual(again.json()["revision"], 9)

    def test_delete_before_first_save_blocks_in_flight_insert(self):
        response = self.client.delete(f"{self.url}/{self.draft_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._put().status_code, 410)
        self.assertEqual(self.client.get(self.url).json(), [])

    def test_api_cannot_discard_background_active_draft_but_backend_can_confirm_it(self):
        from app.application import auto_pilot

        self._put()
        temporary_registry = auto_pilot.AutoPilotRegistry()
        temporary_registry.start(self.novel_id)
        with patch.object(auto_pilot, "registry", temporary_registry):
            response = self.client.delete(f"{self.url}/{self.draft_id}")
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["code"], "draft_still_running")
            self.assertIn("可先复制", response.json()["detail"])
            saved = self._rows()["autopilot_draft"][0]
            self.assertFalse(saved["deleted"])
            # 服务端确认正文提交后可以正常清理自己这一份缓存。
            self.assertTrue(AutopilotDraftRepo(self.db).delete(self.novel_id, self.draft_id)["deleted"])

    def test_failed_history_can_be_discarded_while_different_chapter_runs(self):
        from app.application import auto_pilot

        self._put(state="failed", reason="上次生成失败")
        temporary_registry = auto_pilot.AutoPilotRegistry()
        temporary_registry.start(self.novel_id)
        with patch.object(auto_pilot, "registry", temporary_registry):
            self.assertEqual(self.client.delete(f"{self.url}/{self.draft_id}").status_code, 200)

    def test_client_cannot_overwrite_server_active_progress_but_new_fallback_id_can_save(self):
        from app.application import auto_pilot

        self._put()
        temporary_registry = auto_pilot.AutoPilotRegistry()
        temporary_registry.start(self.novel_id)
        with patch.object(auto_pilot, "registry", temporary_registry):
            response = self._put(revision=9, content="迟到的浏览器本地旧片段", state="unconfirmed")
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["code"], "draft_still_running")
            response = self._put(draft_id=str(uuid.uuid4()), state="unconfirmed")
            self.assertEqual(response.status_code, 200)
        self.assertEqual(AutopilotDraftRepo(self.db).get_raw(self.novel_id, self.draft_id).content,
                         self.payload["content"])

    def test_success_deletes_only_current_draft_and_preserves_failed_history(self):
        self._put(state="failed", reason="第一轮输出上限")
        second_id = str(uuid.uuid4())
        self._put(draft_id=second_id, attempt=2, content="第二轮正文")
        self.client.delete(f"{self.url}/{second_id}")
        recovered = self.client.get(self.url).json()
        self.assertEqual([row["id"] for row in recovered], [self.draft_id])
        self.assertEqual(recovered[0]["state"], "failed")
        self.assertEqual(recovered[0]["content"], self.payload["content"])
        self.assertEqual(AutopilotDraftRepo(self.db).next_attempt(self.novel_id), 3)
        self.assertEqual(AutopilotDraftRepo(self.db).next_attempt(self.other_id), 1)

    def test_missing_novel_cannot_list_save_or_delete_draft(self):
        missing_url = f"/api/v1/novels/{uuid.uuid4()}/autopilot/drafts"
        for response in (self.client.get(missing_url), self._put(url=missing_url),
                         self.client.delete(f"{missing_url}/{self.draft_id}")):
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["code"], "novel_not_found")
        self.assertEqual(self._rows()["autopilot_draft"], [])

    def test_bad_payloads_uuid_and_delete_revision_rejected_without_writes(self):
        for changes in (
            {"content": 23}, {"content": None}, {"attempt": 0}, {"attempt": True},
            {"attempt": "1"}, {"revision": -1}, {"revision": 1.5}, {"node_id": " "},
            {"revision": 2**64}, {"attempt": 2**64},
            {"state": "finalized"}, {"novel_id": self.other_id}, {"title": "a" * 201},
            {"reason": "a" * 4001}, {"content": "a" * 1_000_001},
        ):
            with self.subTest(changes=list(changes)):
                response = self._put(**changes)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], "draft_invalid")
        self.assertEqual(self._put(draft_id="invalid-id").status_code, 422)
        self.assertEqual(self.client.delete(f"{self.url}/{self.draft_id}?revision=0").status_code, 422)
        self.assertEqual(self._rows()["autopilot_draft"], [])

    def test_draft_lifecycle_changes_no_novel_chapter_revision_or_story_bible(self):
        self.db.add_all([
            models.Chapter(novel_id=self.novel_id, title="完整章节", content="真实正文"),
            models.Character(novel_id=self.novel_id, name="事实人物"),
            models.MemoryChunk(novel_id=self.novel_id, text="已有记忆"),
        ])
        self.db.commit()
        before = self._rows()
        self._put()
        self._put(revision=2, state="failed", reason="未完成")
        self.client.get(self.url)
        self.client.delete(f"{self.url}/{self.draft_id}")
        after = self._rows()
        for name in before:
            if name != "autopilot_draft":
                self.assertEqual(after[name], before[name], name)

    def test_delete_novel_removes_recovery_records_and_tombstones(self):
        self._put()
        self.client.delete(f"{self.url}/{uuid.uuid4()}")
        self._put(url=self.other_url, draft_id=str(uuid.uuid4()))
        NovelRepo(self.db).delete(self.novel_id)
        self.assertEqual([row["novel_id"] for row in self._rows()["autopilot_draft"]], [self.other_id])

    def test_repository_rejects_bad_data_and_is_reusable_after_conflict(self):
        repo = AutopilotDraftRepo(self.db)
        repo.upsert(self.novel_id, self.draft_id, **self.payload)
        with self.assertRaises(DraftStorageError) as caught:
            repo.upsert(self.novel_id, self.draft_id, **{**self.payload, "content": "冲突"})
        self.assertEqual(caught.exception.code, "draft_stale")
        changed = repo.upsert(self.novel_id, self.draft_id, **{**self.payload, "revision": 2, "content": "有效片段"})
        self.assertEqual(changed["content"], "有效片段")
        with self.assertRaises(DraftStorageError):
            repo.upsert(self.novel_id, self.draft_id, **{**self.payload, "revision": True})


if __name__ == "__main__":
    unittest.main()
