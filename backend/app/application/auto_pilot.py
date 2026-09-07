"""自动驾驶用例：大纲驱动多章推进 + 暂停/恢复 + 张力评分/文风漂移。"""
import json
import re
import threading
import time

from sqlalchemy.orm import Session

from app.application.consistency import consistency_check
from app.application.context import build_context
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
        yield _sse({"type": "error", "message": str(exc)})
        return

    db = session_factory()
    try:
        nodes = _ordered_chapter_nodes(db, novel_id)
        total = len(nodes)
        done = 0
        yield _sse({"type": "progress", "message": f"开始自动驾驶，共 {total} 个章节点", "total": total, "done": done})

        for node in nodes:
            if state.paused:
                yield _sse({"type": "paused", "message": "已暂停"})
                while state.paused and not state.stop:
                    time.sleep(0.5)
            if state.stop:
                yield _sse({"type": "done", "message": "已停止"})
                return

            if _has_chapter(db, novel_id, node.id):
                yield _sse({"type": "progress", "message": f"跳过已生成：{node.title}", "node_id": node.id})
                continue

            state.current_node_id = node.id
            directive = state.injected_directive
            state.injected_directive = ""
            yield _sse({"type": "progress", "message": f"生成：{node.title}", "node_id": node.id})

            try:
                context = build_context(db, novel_id, directive, f"大纲章节：{node.title}")
                messages = [
                    {"role": "system", "content": WRITE_SYSTEM},
                    {
                        "role": "user",
                        "content": f"{context}\n\n请撰写本章正文（标题：{node.title}；概要：{node.summary or '无'}）：",
                    },
                ]

                full = ""
                for chunk in llm.stream_complete(messages):
                    full += chunk
                    yield _sse({"type": "text", "content": chunk, "node_id": node.id})

                conflicts = consistency_check(db, novel_id, full)["conflicts"]
                yield _sse(
                    {
                        "type": "check",
                        "conflicts": len(conflicts),
                        "node_id": node.id,
                        "message": f"发现 {len(conflicts)} 处冲突" if conflicts else "一致性检查通过",
                    }
                )

                seq = ChapterRepo(db).next_seq(novel_id)
                chapter = ChapterRepo(db).create(
                    novel_id,
                    outline_node_id=node.id,
                    seq=seq,
                    title=node.title,
                    content=full,
                    status="draft",
                    directive=directive,
                )
                state.current_seq = seq
                yield _sse(
                    {"type": "chapter_done", "chapter_id": chapter.id, "seq": seq, "node_id": node.id, "title": node.title}
                )

                try:
                    score = score_tension(full)
                    ChapterRepo(db).update(novel_id, chapter.id, tension_score=score)
                    yield _sse({"type": "tension", "score": round(score, 1), "seq": seq, "chapter_id": chapter.id})
                except Exception as exc:  # noqa: BLE001
                    yield _sse({"type": "error", "message": f"张力评分失败：{exc}", "seq": seq})

                try:
                    sim = check_drift(db, novel_id, full)
                    if sim is not None:
                        yield _sse(
                            {
                                "type": "drift",
                                "score": round(sim, 3),
                                "warning": sim < DRIFT_THRESHOLD,
                                "seq": seq,
                                "chapter_id": chapter.id,
                            }
                        )
                except Exception as exc:  # noqa: BLE001
                    yield _sse({"type": "error", "message": f"文风检测失败：{exc}", "seq": seq})

                state.fail_count = 0
                done += 1
                yield _sse({"type": "progress", "message": f"已完成 {done}/{total}", "done": done, "total": total})

            except Exception as exc:  # noqa: BLE001  单章失败，计数并可能熔断
                state.fail_count += 1
                yield _sse({"type": "error", "message": f"生成失败（第 {state.fail_count} 次）：{exc}", "node_id": node.id})
                if state.fail_count >= FAIL_THRESHOLD:
                    yield _sse({"type": "paused", "message": f"连续失败超过阈值，自动暂停：{exc}"})
                    state.paused = True
                    break

        if not state.stop:
            yield _sse({"type": "done", "message": "自动驾驶完成", "total": total, "done": done})

    finally:
        registry.finish(novel_id)
        db.close()
