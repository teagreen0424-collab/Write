"""自动驾驶用例：大纲驱动多章推进 + 暂停/恢复 + 张力评分/文风漂移。"""
import json
import re
import threading
import time

from sqlalchemy.orm import Session

from app.application.consistency import consistency_check
from app.application.context import build_context
from app.config import get_settings
from app.domain.entities import OutlineLevel
from app.infrastructure import models
from app.infrastructure.ai.llm_provider import LLMProvider
from app.infrastructure.ai.prompts import (
    DRIFT_SYSTEM,
    DRIFT_USER,
    TENSION_SYSTEM,
    TENSION_USER,
    WRITE_SYSTEM,
)
from app.infrastructure.db import SessionLocal
from app.infrastructure.repository import ChapterRepo, NovelRepo, OutlineRepo

llm = LLMProvider()

FAIL_THRESHOLD = 3
DRIFT_THRESHOLD = 0.6


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


class AutoPilotBusy(Exception):
    """同一作品已有自动驾驶任务在运行。"""


class AutoPilotState:
    def __init__(self) -> None:
        self.paused = False
        self.stop = False
        self.fail_count = 0
        self.current_node_id = ""
        self.current_seq = 0
        self.injected_directive = ""


class AutoPilotRegistry:
    """内存态运行注册表（单用户本地，进程重启后由落库数据兜底恢复）。"""

    def __init__(self) -> None:
        self._states: dict[str, AutoPilotState] = {}
        self._lock = threading.Lock()

    def start(self, novel_id: str) -> AutoPilotState:
        with self._lock:
            if novel_id in self._states:
                raise AutoPilotBusy("该作品已有自动驾驶任务在运行")
            state = AutoPilotState()
            self._states[novel_id] = state
            return state

    def is_running(self, novel_id: str) -> bool:
        with self._lock:
            return novel_id in self._states

    def get(self, novel_id: str):
        return self._states.get(novel_id)

    def pause(self, novel_id: str):
        state = self._states.get(novel_id)
        if state:
            state.paused = True
        return state

    def resume(self, novel_id: str):
        state = self._states.get(novel_id)
        if state:
            state.paused = False
        return state

    def inject(self, novel_id: str, directive: str):
        state = self._states.get(novel_id)
        if state:
            state.injected_directive = directive
        return state

    def finish(self, novel_id: str) -> None:
        with self._lock:
            self._states.pop(novel_id, None)


registry = AutoPilotRegistry()


def _ordered_chapter_nodes(db: Session, novel_id: str):
    """按 卷 → 幕 → 章 的树序返回所有章节点。"""
    nodes = OutlineRepo(db).list_all(novel_id)
    by_parent: dict[str, list] = {}
    for n in nodes:
        by_parent.setdefault(n.parent_id, []).append(n)
    for children in by_parent.values():
        children.sort(key=lambda n: n.sort_order)

    chapters: list = []

    def walk(parent_id: str) -> None:
        for n in by_parent.get(parent_id, []):
            if n.level == OutlineLevel.CHAPTER.value:
                chapters.append(n)
            walk(n.id)

    walk("")
    return chapters


def _has_chapter(db: Session, novel_id: str, outline_node_id: str) -> bool:
    return (
        db.query(models.Chapter)
        .filter(
            models.Chapter.novel_id == novel_id,
            models.Chapter.outline_node_id == outline_node_id,
        )
        .first()
        is not None
    )


def _parse_number(raw: str) -> float:
    m = re.search(r"[-+]?\d+(?:\.\d+)?", raw or "")
    if not m:
        raise ValueError("无法解析分数")
    return float(m.group())


def score_tension(content: str) -> float:
    """调用 LLM 对正文做 0-10 张力评分。"""
    raw = llm.complete(
        [
            {"role": "system", "content": TENSION_SYSTEM},
            {"role": "user", "content": TENSION_USER.format(content=content)},
        ],
        temperature=0.2,
        max_tokens=10,
    )
    return max(0.0, min(10.0, _parse_number(raw)))


def check_drift(db: Session, novel_id: str, content: str):
    """与 novel.style_anchor 比对文风相似度，返回 0-1；无锚定则返回 None。"""
    novel = NovelRepo(db).get(novel_id)
    anchor = (novel.style_anchor or "").strip()
    if not anchor:
        return None
    raw = llm.complete(
        [
            {"role": "system", "content": DRIFT_SYSTEM},
            {"role": "user", "content": DRIFT_USER.format(anchor=anchor, content=content)},
        ],
        temperature=0.2,
        max_tokens=10,
    )
    return max(0.0, min(1.0, _parse_number(raw)))


def get_status(novel_id: str):
    state = registry.get(novel_id)
    if not state:
        return None
    return {
        "running": True,
        "paused": state.paused,
        "fail_count": state.fail_count,
        "current_node_id": state.current_node_id,
        "current_seq": state.current_seq,
    }


def run_autopilot(novel_id: str, session_factory=SessionLocal):
    """自动驾驶主循环（SSE 生成器，运行在独立线程）。"""
    try:
        state = registry.start(novel_id)
    except AutoPilotBusy as exc:
        yield _sse({
            "type": "error", "scope": "task", "fatal": True,
            "code": "autopilot_busy", "message": str(exc),
        })
        return

    db = None
    generated = skipped = failed = total = 0
    terminal_status = "completed"
    terminal_reason = ""
    task_error = None
    cleanup_error = None

    def counts():
        return {
            "generated": generated, "skipped": skipped, "failed": failed,
            "done": generated + skipped, "total": total,
        }

    try:
        db = session_factory()
        nodes = _ordered_chapter_nodes(db, novel_id)
        total = len(nodes)
        yield _sse({
            "type": "progress", "phase": "starting",
            "message": f"开始自动驾驶，共 {total} 个章节点", **counts(),
        })

        for node in nodes:
            if state.paused:
                yield _sse({"type": "paused", "message": "已暂停"})
                while state.paused and not state.stop:
                    time.sleep(0.5)
                    if state.paused and not state.stop:
                        # 保留可取消的输出边界，暂停时断开客户端也能回收任务。
                        yield ": paused\n\n"
            if state.stop:
                terminal_status = "stopped"
                break

            # 缓存节点标识，事务回滚后仍能明确指出失败的章节。
            node_id, node_title = node.id, node.title
            if _has_chapter(db, novel_id, node_id):
                skipped += 1
                yield _sse({
                    "type": "progress", "phase": "skipped",
                    "message": f"跳过已生成：{node_title}", "node_id": node_id,
                    "title": node_title, **counts(),
                })
                continue

            state.current_node_id = node_id
            directive = state.injected_directive
            state.injected_directive = ""
            yield _sse({
                "type": "progress", "phase": "generating",
                "message": f"生成：{node_title}", "node_id": node_id,
                "title": node_title, **counts(),
            })

            try:
                context = build_context(db, novel_id, directive, f"大纲章节：{node_title}")
                messages = [
                    {"role": "system", "content": WRITE_SYSTEM},
                    {
                        "role": "user",
                        "content": f"{context}\n\n请撰写本章正文（标题：{node_title}；概要：{node.summary or '无'}）：",
                    },
                ]

                full = ""
                for chunk in llm.stream_complete(messages, max_tokens=get_settings().writing_max_tokens):
                    full += chunk
                    yield _sse({
                        "type": "text", "content": chunk, "node_id": node_id, "title": node_title,
                    })

                if not full.strip():
                    raise ValueError("未生成有效正文，请重试")

                conflicts = consistency_check(db, novel_id, full)["conflicts"]
                yield _sse(
                    {
                        "type": "check",
                        "conflicts": len(conflicts),
                        "node_id": node_id,
                        "title": node_title,
                        "message": f"发现 {len(conflicts)} 处冲突" if conflicts else "一致性检查通过",
                    }
                )

                seq = ChapterRepo(db).next_seq(novel_id)
                chapter = ChapterRepo(db).create(
                    novel_id,
                    outline_node_id=node_id,
                    seq=seq,
                    title=node_title,
                    content=full,
                    status="draft",
                    directive=directive,
                )
            except Exception as exc:  # noqa: BLE001  单章失败，计数并可能熔断
                db.rollback()
                failed += 1
                state.fail_count += 1
                yield _sse({
                    "type": "error", "scope": "chapter", "fatal": False,
                    "code": getattr(exc, "code", "generation_failed"), "saved": False,
                    "message": f"《{node_title}》生成失败（连续第 {state.fail_count} 次）：{exc} 本章未保存，已收到的内容保留为未完成稿。",
                    "node_id": node_id, "title": node_title, **counts(),
                })
                if state.fail_count >= FAIL_THRESHOLD:
                    terminal_reason = "failure_threshold"
                    break
                continue

            # 正文已经提交：质量检测失败不能误报为正文失败，也不能撤销此确认。
            chapter_id = chapter.id
            state.current_seq = seq
            state.fail_count = 0
            generated += 1
            yield _sse({
                "type": "chapter_done", "chapter_id": chapter_id, "seq": seq,
                "node_id": node_id, "title": node_title, **counts(),
            })

            try:
                score = score_tension(full)
                ChapterRepo(db).update(novel_id, chapter_id, tension_score=score)
                yield _sse({
                    "type": "tension", "score": round(score, 1), "seq": seq,
                    "chapter_id": chapter_id, "node_id": node_id, "title": node_title,
                })
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                yield _sse({
                    "type": "error", "scope": "quality", "fatal": False,
                    "code": "quality_check_failed", "kind": "tension", "saved": True,
                    "message": f"《{node_title}》正文已保存，张力评分失败：{exc}",
                    "seq": seq, "chapter_id": chapter_id, "node_id": node_id, "title": node_title,
                })

            try:
                sim = check_drift(db, novel_id, full)
                if sim is not None:
                    ChapterRepo(db).update(novel_id, chapter_id, drift_score=sim)
                    yield _sse({
                        "type": "drift", "score": round(sim, 3), "warning": sim < DRIFT_THRESHOLD,
                        "seq": seq, "chapter_id": chapter_id, "node_id": node_id, "title": node_title,
                    })
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                yield _sse({
                    "type": "error", "scope": "quality", "fatal": False,
                    "code": "quality_check_failed", "kind": "drift", "saved": True,
                    "message": f"《{node_title}》正文已保存，文风检测失败：{exc}",
                    "seq": seq, "chapter_id": chapter_id, "node_id": node_id, "title": node_title,
                })

            yield _sse({
                "type": "progress", "phase": "generated",
                "message": f"已完成 {generated + skipped}/{total}", "node_id": node_id,
                "title": node_title, **counts(),
            })
        if state.stop:
            terminal_status = "stopped"
        elif generated + skipped < total:
            terminal_status = "incomplete"
    except Exception as exc:  # noqa: BLE001  任务启动/编排异常也必须明确结束。
        terminal_status = "incomplete"
        if db is not None:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001  回滚失败不能掩盖任务终态。
                pass
        task_error = {
            "type": "error", "scope": "task", "fatal": True,
            "code": "autopilot_task_failed", "message": f"自动驾驶任务异常结束：{exc}",
        }
    finally:
        registry.finish(novel_id)
        if db is not None:
            try:
                db.close()
            except Exception as exc:  # noqa: BLE001
                cleanup_error = exc

    if task_error is not None:
        yield _sse(task_error)
    if cleanup_error is not None:
        terminal_status = "incomplete"
        yield _sse({
            "type": "error", "scope": "task", "fatal": True,
            "code": "autopilot_cleanup_failed", "message": f"自动驾驶会话关闭失败：{cleanup_error}",
        })
    message = {
        "completed": "自动驾驶完成",
        "incomplete": "自动驾驶结束，仍有未完成章节",
        "stopped": "已停止",
    }[terminal_status]
    if terminal_reason == "failure_threshold":
        message = "连续三次生成失败，自动驾驶已结束，仍有未完成章节"
    yield _sse({
        "type": "done", "status": terminal_status, "message": message,
        "reason": terminal_reason, **counts(),
    })
