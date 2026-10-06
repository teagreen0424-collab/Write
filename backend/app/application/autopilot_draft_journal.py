"""生成片段先独立提交恢复缓存，再发送给浏览器。"""
from uuid import uuid4

from app.infrastructure.autopilot_drafts import AutopilotDraftRepo


class AutopilotDraftJournal:
    def __init__(self, session_factory, novel_id: str):
        self.db = session_factory()
        self.novel_id = novel_id
        self.repo = AutopilotDraftRepo(self.db)
        self.current = None
        self.pending_cleanup = set()
        try:
            self.attempt = self.repo.next_attempt(novel_id)
        except Exception:
            self.db.close()
            raise

    def _save(self, state: str, reason: str = ""):
        current = self.current
        current["revision"] += 1
        self.repo.upsert(
            self.novel_id, current["id"],
            node_id=current["node_id"], title=current["title"], attempt=self.attempt,
            content=current["content"], state=state, reason=reason[:4000],
            revision=current["revision"],
        )
        return self.metadata()

    def metadata(self):
        if self.current is None:
            return {}
        return {
            "draft_id": self.current["id"], "draft_revision": self.current["revision"],
            "draft_attempt": self.attempt, "draft_persisted": True,
        }

    def start(self, node_id: str, title: str):
        self.current = {"id": str(uuid4()), "node_id": node_id, "title": title, "content": "", "revision": 0}
        return self._save("active")

    def append(self, text: str):
        self.current["content"] += text
        return self._save("active")

    def fail(self, reason: str):
        metadata = self._save("failed", reason)
        self.current = None
        return metadata

    def saved(self):
        metadata = self.metadata()
        draft_id = self.current["id"]
        self.pending_cleanup.add(draft_id)
        try:
            deletion = self.repo.delete(self.novel_id, draft_id)
            metadata["draft_revision"] = deletion["revision"]
            self.pending_cleanup.discard(draft_id)
        finally:
            # 清理异常不阻塞后续章，也不能被下一次 start 覆盖掉待清理记录。
            self.current = None
        return metadata

    def close(self):
        error = None
        try:
            if self.current is not None:
                try:
                    self._save("unconfirmed", "生成连接已结束，章节保存状态尚未确认；请核对章节后继续。")
                    self.current = None
                except Exception as exc:
                    error = exc
            for draft_id in list(self.pending_cleanup):
                try:
                    self.repo.delete(self.novel_id, draft_id)
                    self.pending_cleanup.discard(draft_id)
                except Exception as exc:
                    error = error or exc
            if error is not None:
                raise error
        finally:
            self.db.close()
