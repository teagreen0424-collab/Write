"""独立任务路由验收：幂等启动、控制身份、事件重连与真实ASGI断开。"""
import asyncio
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.application import auto_pilot, autopilot_tasks
from app.infrastructure import models
from app.infrastructure.db import get_db
from app.infrastructure import repository as repos
from app.interfaces.api.v1.autopilot import router
from app.interfaces.api.v1.novels import router as novels_router
from app.interfaces.schemas import StoryCheckResult


def _events(body):
    result = []
    for frame in body.replace("\r\n", "\n").split("\n\n"):
        payload = "\n".join(line[5:].strip() for line in frame.splitlines() if line.startswith("data:"))
        if payload:
            result.append(json.loads(payload))
    return result


class AutopilotTaskRouteTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database = (Path(directory.name) / "task-routes.db").as_posix()
        self.engine = create_engine(f"sqlite:///{database}", connect_args={"check_same_thread": False})
        models.Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.sessions()
        self.addCleanup(self.db.close)
        self.novel_id = repos.NovelRepo(self.db).create(title="任务API作品").id
        self.other_id = repos.NovelRepo(self.db).create(title="不可跨作用域作品").id
        self.node = repos.OutlineRepo(self.db).create(self.novel_id, level="chapter", title="API验收章")
        self.registry = auto_pilot.AutoPilotRegistry()
        self.manager = autopilot_tasks.AutopilotTaskManager(
            session_factory=self.sessions, registry=self.registry,
            heartbeat_seconds=0.02, shutdown_timeout=2,
        )
        self.blocked = threading.Event()
        self.release = threading.Event()
        for mocked in (
            patch.object(autopilot_tasks, "manager", self.manager),
            patch.object(auto_pilot, "registry", self.registry),
            patch.object(auto_pilot, "build_context", return_value="隔离API上下文"),
            patch.object(auto_pilot, "review_story", return_value=StoryCheckResult()),
            patch.object(auto_pilot, "consistency_check", return_value={"conflicts": []}),
            patch.object(auto_pilot, "score_tension", return_value=5.0),
            patch.object(auto_pilot, "check_drift", return_value=None),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        self.app = FastAPI()
        self.app.include_router(router, prefix="/api/v1")
        self.app.include_router(novels_router, prefix="/api/v1")

        def override_db():
            with self.sessions() as db:
                yield db

        self.app.dependency_overrides[get_db] = override_db
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.addCleanup(self._shutdown)
        self.url = f"/api/v1/novels/{self.novel_id}/autopilot"
        self.other_url = f"/api/v1/novels/{self.other_id}/autopilot"

    def _shutdown(self):
        self.release.set()
        state = self.registry.get(self.novel_id)
        if state is not None:
            state.paused = False
            state.stop = True
        self.manager.shutdown()

    def _gated_body(self, *args, **kwargs):
        yield "HTTP断开前已接收的中文片段。"
        self.blocked.set()
        if not self.release.wait(5):
            raise RuntimeError("API测试正文门闩未释放")
        yield "HTTP断开后后台继续完成。"

    def _start(self, request_id=None):
        request_id = request_id or str(uuid.uuid4())
        response = self.client.post(self.url + "/start", json={"request_id": request_id})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["task_id"], request_id)
        return request_id, response.json()

    def _terminal(self, task_id):
        self.assertTrue(self.manager.wait(task_id, timeout=5))
        response = self.client.get(self.url + "/status")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["running"])
        return response.json()

    def _rows(self):
        with self.sessions() as observer:
            return {table.name: sorted(
                [dict(row) for row in observer.execute(select(table)).mappings()],
                key=lambda row: str(row.get("id", "")),
            ) for table in models.Base.metadata.sorted_tables}

    def test_get_status_and_legacy_sse_are_read_only_and_cannot_start_a_task(self):
        before = self._rows()
        with patch.object(auto_pilot.llm, "stream_complete") as provider:
            status = self.client.get(self.url + "/status")
            self.assertEqual(status.json(), {"running": False})
            stream = self.client.get(self.url + "/sse")
            self.assertEqual(stream.status_code, 404)
            self.assertEqual(set(stream.json()), {"detail", "code"})
            provider.assert_not_called()
        self.assertEqual(self._rows(), before)

    def test_start_is_idempotent_and_conflict_identifies_active_task(self):
        with patch.object(auto_pilot.llm, "stream_complete", side_effect=self._gated_body) as provider:
            task_id, _ = self._start()
            self.assertTrue(self.blocked.wait(3))
            repeated = self.client.post(self.url + "/start", json={"request_id": task_id})
            self.assertEqual(repeated.status_code, 200)
            self.assertEqual(repeated.json()["task_id"], task_id)
            conflict = self.client.post(self.url + "/start", json={"request_id": str(uuid.uuid4())})
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(conflict.json()["task_id"], task_id)
            self.assertEqual(set(conflict.json()), {"detail", "code", "task_id"})
            self.release.set()
            self._terminal(task_id)
            provider.assert_called_once()

    def test_bad_start_payload_missing_novel_and_invalid_task_path_never_generate(self):
        before = self._rows()
        with patch.object(auto_pilot.llm, "stream_complete") as provider:
            for payload in ({}, {"request_id": "bad-uuid"}, {"request_id": None}):
                invalid = self.client.post(self.url + "/start", json=payload)
                self.assertEqual(invalid.status_code, 422)
                self.assertEqual(set(invalid.json()), {"detail", "code"})
            missing = self.client.post(
                f"/api/v1/novels/{uuid.uuid4()}/autopilot/start", json={"request_id": str(uuid.uuid4())},
            )
            self.assertEqual(missing.status_code, 404)
            self.assertIn("code", missing.json())
            malformed = self.client.get(self.url + "/tasks/not-a-uuid/events")
            self.assertEqual(malformed.status_code, 422)
            provider.assert_not_called()
        self.assertEqual(self._rows(), before)

    def test_completed_events_snapshot_cursor_replay_and_done_fallback_do_not_call_model_again(self):
        with patch.object(auto_pilot.llm, "stream_complete", return_value=iter(["第一段。", "第二段。"])) as provider:
            task_id, _ = self._start()
            terminal = self._terminal(task_id)
            before = self._rows()
            path = self.url + f"/tasks/{task_id}/events"
            fresh = self.client.get(path)
            self.assertEqual(fresh.status_code, 200)
            self.assertTrue(fresh.headers["content-type"].startswith("text/event-stream"))
            snapshot = _events(fresh.text)
            self.assertEqual(snapshot[0]["type"], "snapshot")
            self.assertEqual(snapshot[0]["task_id"], task_id)
            self.assertEqual(snapshot[-1]["type"], "done")
            history = _events(self.client.get(path + "?after=0").text)
            self.assertEqual("".join(event["content"] for event in history if event["type"] == "text"),
                             "第一段。第二段。")
            event_ids = [event["event_id"] for event in history]
            self.assertEqual(event_ids, sorted(set(event_ids)))
            done = _events(self.client.get(path + f"?after={terminal['event_id']}").text)
            self.assertEqual(len(done), 1)
            self.assertEqual(done[0]["type"], "done")
            legacy = _events(self.client.get(self.url + "/sse").text)
            self.assertEqual(legacy[0]["task_id"], task_id)
            provider.assert_called_once()
        self.assertEqual(self._rows(), before)

    def test_events_reject_negative_future_cursor_and_cross_novel_before_streaming_headers(self):
        with patch.object(auto_pilot.llm, "stream_complete", side_effect=self._gated_body):
            task_id, _ = self._start()
            self.assertTrue(self.blocked.wait(3))
            path = self.url + f"/tasks/{task_id}/events"
            for value in ("-1", "bad", "1.5"):
                response = self.client.get(path + "?after=" + value)
                self.assertEqual(response.status_code, 422)
                self.assertIn("code", response.json())
            ahead = self.client.get(path + "?after=9999999")
            self.assertEqual(ahead.status_code, 409)
            self.assertEqual(ahead.json()["task_id"], task_id)
            self.assertTrue(ahead.headers["content-type"].startswith("application/json"))
            cross = self.client.get(self.other_url + f"/tasks/{task_id}/events")
            self.assertEqual(cross.status_code, 404)
            self.assertIn("code", cross.json())
            self.release.set()
            self._terminal(task_id)

    def test_controls_require_current_task_identity_and_return_persisted_event_identity(self):
        with patch.object(auto_pilot.llm, "stream_complete", side_effect=self._gated_body):
            task_id, _ = self._start()
            self.assertTrue(self.blocked.wait(3))
            pause = self.client.post(self.url + f"/tasks/{task_id}/pause")
            self.assertEqual(pause.status_code, 200)
            self.assertEqual(pause.json()["task_id"], task_id)
            self.assertGreater(pause.json()["event_id"], 0)
            self.assertTrue(self.client.get(self.url + "/status").json()["pause_requested"])
            injected = self.client.post(self.url + f"/tasks/{task_id}/inject", json={"directive": "作者追加要求"})
            self.assertEqual(injected.status_code, 200)
            self.assertEqual(self.registry.get(self.novel_id).injected_directive, "作者追加要求")
            for action in ("pause", "resume", "inject"):
                body = {"directive": "越界要求"} if action == "inject" else None
                cross = self.client.post(self.other_url + f"/tasks/{task_id}/{action}", json=body)
                self.assertEqual(cross.status_code, 404)
                self.assertIn("code", cross.json())
            legacy_missing_identity = self.client.post(self.url + "/resume")
            self.assertEqual(legacy_missing_identity.status_code, 422)
            resumed = self.client.post(self.url + f"/tasks/{task_id}/resume")
            self.assertEqual(resumed.status_code, 200)
            self.assertFalse(self.client.get(self.url + "/status").json()["pause_requested"])
            self.release.set()
            self._terminal(task_id)
            stale = self.client.post(self.url + f"/tasks/{task_id}/pause")
            self.assertIn(stale.status_code, (404, 409))

    async def test_asgi_http_disconnect_after_first_text_keeps_actual_kernel_worker_running(self):
        with patch.object(auto_pilot.llm, "stream_complete", side_effect=self._gated_body) as provider:
            task_id, _ = self._start()
            self.assertTrue(self.blocked.wait(3))
            disconnect = asyncio.Event()
            messages = []
            first_request = True

            async def receive():
                nonlocal first_request
                if first_request:
                    first_request = False
                    return {"type": "http.request", "body": b"", "more_body": False}
                await disconnect.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                messages.append(message)
                if message["type"] == "http.response.body":
                    chunk = message.get("body", b"").decode("utf-8")
                    if any(event["type"] == "text" for event in _events(chunk)):
                        disconnect.set()

            path = self.url + f"/tasks/{task_id}/events"
            scope = {
                "type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"},
                "http_version": "1.1", "method": "GET", "scheme": "http", "path": path,
                "raw_path": path.encode(), "query_string": b"after=0", "root_path": "",
                "headers": [(b"host", b"testserver")],
                "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
            }
            await asyncio.wait_for(self.app(scope, receive, send), timeout=3)
            self.assertTrue(disconnect.is_set())
            self.assertEqual(next(message["status"] for message in messages if message["type"] == "http.response.start"), 200)
            status = self.client.get(self.url + "/status").json()
            self.assertTrue(status["running"])
            self.assertEqual(status["task_id"], task_id)
            self.assertTrue(self.registry.is_running(self.novel_id))
            self.release.set()
            terminal = self._terminal(task_id)
            self.assertEqual(terminal["status"], "completed")
            reconnect = _events(self.client.get(path).text)
            self.assertEqual(reconnect[0]["type"], "snapshot")
            self.assertEqual(reconnect[-1]["type"], "done")
            provider.assert_called_once()
        self.assertEqual(self._rows()["chapter"][0]["content"],
                         "HTTP断开前已接收的中文片段。HTTP断开后后台继续完成。")

    def test_novel_delete_is_blocked_while_worker_runs_and_cascades_tasks_after_completion(self):
        path = f"/api/v1/novels/{self.novel_id}"
        with patch.object(auto_pilot.llm, "stream_complete", side_effect=self._gated_body) as provider:
            task_id, _ = self._start()
            self.assertTrue(self.blocked.wait(3))
            before = self._rows()
            busy = self.client.delete(path)
            self.assertEqual(busy.status_code, 409)
            self.assertEqual(busy.json()["code"], "autopilot_busy")
            self.assertEqual(self._rows(), before)
            self.release.set()
            self._terminal(task_id)
            deleted = self.client.delete(path)
            self.assertEqual(deleted.status_code, 204)
            provider.assert_called_once()
        rows = self._rows()
        self.assertFalse(any(row["id"] == self.novel_id for row in rows["novel"]))
        for table in ("chapter", "outline_node", "autopilot_draft", "autopilot_task", "autopilot_task_event"):
            self.assertFalse(any(row["novel_id"] == self.novel_id for row in rows[table]), table)
        self.assertTrue(any(row["id"] == self.other_id for row in rows["novel"]))


if __name__ == "__main__":
    unittest.main()
