"""自动驾驶用例：大纲驱动多章推进 + 暂停/恢复 + 张力评分/文风漂移。"""
import json
import re
import threading
import time

from sqlalchemy.orm import Session

from app.application.consistency import consistency_check
from app.application.autopilot_draft_journal import AutopilotDraftJournal
from app.application.context import build_context, ordered_chapter_nodes, prior_chapter_context
from app.application.story_review import review_story
from app.config import get_settings
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
from app.infrastructure.repository import ChapterRepo, NovelRepo

llm = LLMProvider()

FAIL_THRESHOLD = 3
DRIFT_THRESHOLD = 0.6


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


class AutoPilotBusy(Exception):
    """同一作品已有自动驾驶任务在运行。"""


class AutoPilotOrderError(Exception):
    """已有章节的位置存在歧义，不能自动猜测或覆盖作者的排序。"""

    code = "autopilot_order_ambiguous"


class AutoPilotState:
    def __init__(self, owner_id: str = "") -> None:
        self.owner_id = owner_id
        self.paused = False
        self.stop = False
        self.fail_count = 0
        self.current_node_id = ""
        self.current_seq = 0
        self.injected_directive = ""
        self.pause_message = ""


class AutoPilotRegistry:
    """内存态运行注册表（单用户本地，进程重启后由落库数据兜底恢复）。"""

    def __init__(self) -> None:
        self._states: dict[str, AutoPilotState] = {}
        self._lock = threading.Lock()

    def start(self, novel_id: str, owner_id: str = "") -> AutoPilotState:
        with self._lock:
            if novel_id in self._states:
                raise AutoPilotBusy("该作品已有自动驾驶任务在运行")
            state = AutoPilotState(owner_id)
            self._states[novel_id] = state
            return state

    def is_running(self, novel_id: str) -> bool:
        with self._lock:
            return novel_id in self._states

    def get(self, novel_id: str):
        with self._lock:
            return self._states.get(novel_id)

    def pause(self, novel_id: str, owner_id: str | None = None):
        with self._lock:
            state = self._states.get(novel_id)
            if state is not None and (owner_id is None or state.owner_id == owner_id):
                state.paused = True
                return state
            return None

    def resume(self, novel_id: str, owner_id: str | None = None):
        with self._lock:
            state = self._states.get(novel_id)
            if state is not None and (owner_id is None or state.owner_id == owner_id):
                state.paused = False
                state.pause_message = ""
                return state
            return None

    def inject(self, novel_id: str, directive: str, owner_id: str | None = None):
        with self._lock:
            state = self._states.get(novel_id)
            if state is not None and (owner_id is None or state.owner_id == owner_id):
                state.injected_directive = directive
                return state
            return None

    def finish(self, novel_id: str, owner_id: str | None = None) -> None:
        with self._lock:
            state = self._states.get(novel_id)
            if state is not None and (owner_id is None or state.owner_id == owner_id):
                self._states.pop(novel_id, None)


registry = AutoPilotRegistry()


def _ordered_chapter_nodes(db: Session, novel_id: str):
    """按 卷 → 幕 → 章 的树序返回所有章节点。"""
    return ordered_chapter_nodes(db, novel_id)


def _rule_warnings(conflicts: list[dict], title: str, content: str) -> list[str]:
    warnings: list[str] = []
    for conflict in conflicts:
        excerpt = ""
        start, end = conflict.get("start"), conflict.get("end")
        if type(start) is int and type(end) is int and 0 <= start < end <= len(content):
            excerpt = content[start:end]
        reason = conflict.get("message") or "存在设定一致性冲突，请核对正文"
        warnings.append(f"《{title}》正文已保存，规则检查：{reason}"
                        + (f"；原文：『{excerpt}』" if excerpt else ""))
    return warnings


def _refresh_author_context(db: Session) -> None:
    """章节边界重新读取作者改稿；仅允许在没有待提交写入时失效缓存。"""
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("章节切换时存在未提交写入，已停止推进以保护待保存内容。")
    # 暂停期间作者可能通过另一请求会话改稿；不能继续使用长会话identity map旧正文。
    db.expire_all()


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


def _chapter_insert_seq(db: Session, novel_id: str, node_id: str, ordered_node_ids: list[str]) -> int:
    """为缺失的大纲章让出位置；平移与随后的正文创建由调用方一次提交。"""
    chapters = (
        db.query(models.Chapter)
        .filter(models.Chapter.novel_id == novel_id)
        .order_by(models.Chapter.seq)
        .all()
    )
    if any(chapter.outline_node_id == node_id for chapter in chapters):
        raise AutoPilotOrderError("生成期间该大纲章已保存，请重试核对，避免重复创建正文。")
    if len({chapter.seq for chapter in chapters}) != len(chapters):
        raise AutoPilotOrderError("现有章节序号重复，请先调整章节顺序后重试。")

    order = {item_id: index for index, item_id in enumerate(ordered_node_ids)}
    target = order[node_id]
    following = [
        chapter.seq for chapter in chapters
        if chapter.outline_node_id in order and order[chapter.outline_node_id] > target
    ]
    if not following:
        # 没有后续大纲锚点时沿用追加，作者自建章也保持原位。
        return ChapterRepo(db).next_seq(novel_id)

    seq = min(following)
    preceding = [
        chapter.seq for chapter in chapters
        if chapter.outline_node_id in order and order[chapter.outline_node_id] < target
    ]
    if preceding and max(preceding) >= seq:
        raise AutoPilotOrderError("前后大纲章的现有顺序不一致，请先调整章节顺序后重试。")

    # 只给本次新章让位，保持包括无大纲章在内的所有已有章相对顺序。
    # 显式回写原更新时间，避免排序操作改变作者稿件的编辑时间。
    db.query(models.Chapter).filter(
        models.Chapter.novel_id == novel_id,
        models.Chapter.seq >= seq,
    ).update({
        models.Chapter.seq: models.Chapter.seq + 1,
        models.Chapter.updated_at: models.Chapter.updated_at,
    }, synchronize_session="fetch")
    return seq


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


def run_autopilot(novel_id: str, session_factory=SessionLocal, reserved_state: AutoPilotState | None = None):
    """生成内核；后台管理器消费它，浏览器只订阅已提交的任务事件。"""
    try:
        if reserved_state is None:
            state = registry.start(novel_id)
        elif registry.get(novel_id) is reserved_state:
            state = reserved_state
        else:
            yield _sse({
                "type": "error", "scope": "task", "fatal": True,
                "code": "autopilot_task_ownership_lost", "message": "该任务已不再持有本作品的生成权限。",
            })
            return
    except AutoPilotBusy as exc:
        yield _sse({
            "type": "error", "scope": "task", "fatal": True,
            "code": "autopilot_busy", "message": str(exc),
        })
        return

    db = None
    journal = None
    generated = skipped = failed = total = 0
    terminal_status = "completed"
    terminal_reason = ""
    task_error = None
    cleanup_error = None
    has_quality_warning = False

    def counts():
        return {
            "generated": generated, "skipped": skipped, "failed": failed,
            "done": generated + skipped, "total": total,
        }

    try:
        db = session_factory()
        journal = AutopilotDraftJournal(session_factory, novel_id)
        nodes = _ordered_chapter_nodes(db, novel_id)
        ordered_node_ids = [node.id for node in nodes]
        total = len(nodes)
        yield _sse({
            "type": "progress", "phase": "starting",
            "message": f"开始自动驾驶，共 {total} 个章节点", **counts(),
        })

        for node_index, node in enumerate(nodes):
            if state.paused:
                yield _sse({"type": "paused", "message": state.pause_message or "已暂停"})
                while state.paused and not state.stop:
                    time.sleep(0.5)
                    if state.paused and not state.stop:
                        # 后台管理器可在暂停期间检查停服请求，订阅断开不影响此循环。
                        yield ": paused\n\n"
            if state.stop:
                terminal_status = "stopped"
                break

            # 处于上一章提交后、下一章插序/保存前的只读边界；恢复时同步刷新前情和设定。
            _refresh_author_context(db)

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
            draft_meta = {}
            try:
                draft_meta = journal.start(node_id, node_title)
                yield _sse({
                    "type": "progress", "phase": "generating",
                    "message": f"生成：{node_title}", "node_id": node_id,
                    "title": node_title, **counts(), **draft_meta,
                })
                context = build_context(db, novel_id, directive, f"大纲章节：{node_title}",
                                        outline_node_id=node_id)
                previous_context = prior_chapter_context(db, novel_id, outline_node_id=node_id).text
                messages = [
                    {"role": "system", "content": WRITE_SYSTEM},
                    {
                        "role": "user",
                        "content": f"{context}\n\n{previous_context}\n\n请撰写本章正文（标题：{node_title}；概要：{node.summary or '无'}）：",
                    },
                ]

                full = ""
                chunks = iter(llm.stream_complete(messages, max_tokens=get_settings().writing_max_tokens))
                try:
                    while not state.stop:
                        try:
                            chunk = next(chunks)
                        except StopIteration:
                            break
                        if state.stop:
                            break
                        full += chunk
                        draft_meta = journal.append(chunk)
                        yield _sse({
                            "type": "text", "content": chunk, "node_id": node_id, "title": node_title,
                            **draft_meta,
                        })
                finally:
                    close_stream = getattr(chunks, "close", None)
                    if close_stream is not None:
                        close_stream()

                if state.stop:
                    terminal_status = "stopped"
                    break

                if not full.strip():
                    raise ValueError("未生成有效正文，请重试")

                conflicts = consistency_check(db, novel_id, full)["conflicts"]

                if state.stop:
                    terminal_status = "stopped"
                    break

                seq = _chapter_insert_seq(db, novel_id, node_id, ordered_node_ids)
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
                failure_message = f"《{node_title}》生成失败（连续第 {state.fail_count} 次）：{exc} 本章未保存，已收到的内容保留为未完成稿。"
                if journal.current is not None:
                    try:
                        draft_meta = journal.fail(failure_message)
                    except Exception as storage_exc:  # 缓存失败不能伪称刷新后可恢复。
                        draft_meta = {**journal.metadata(), "draft_persisted": False}
                        failure_message += f" 未完成稿自动保存未确认：{storage_exc}，请先复制页面片段。"
                yield _sse({
                    "type": "error", "scope": "chapter", "fatal": False,
                    "code": getattr(exc, "code", "generation_failed"), "saved": False,
                    "message": failure_message,
                    "node_id": node_id, "title": node_title, **counts(), **draft_meta,
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
            draft_cleanup_error = None
            try:
                draft_meta = journal.saved()
            except Exception as exc:
                draft_cleanup_error = exc
            yield _sse({
                "type": "chapter_done", "chapter_id": chapter_id, "seq": seq,
                "node_id": node_id, "title": node_title, **counts(), **draft_meta,
            })
            rule_warnings = _rule_warnings(conflicts, node_title, full)
            yield _sse({
                "type": "check", "conflicts": len(conflicts), "warnings": rule_warnings,
                "node_id": node_id, "title": node_title, "seq": seq,
                "chapter_id": chapter_id, "saved": True,
                "message": f"正文已保存，发现 {len(conflicts)} 处规则冲突" if conflicts else "一致性规则检查通过",
            })
            if draft_cleanup_error is not None:
                # 正文已经成功，缓存清理异常不能冒充正文生成失败。
                yield _sse({
                    "type": "error", "scope": "quality", "fatal": False,
                    "code": "draft_cleanup_failed", "kind": "draft_cleanup", "saved": True,
                    "message": f"《{node_title}》正文已保存，未完成稿缓存清理失败，请核对章节：{draft_cleanup_error}",
                    "node_id": node_id, "title": node_title,
                })

            quality_needs_review = bool(conflicts)
            try:
                result = review_story(
                    db, novel_id, full, directive=directive, chapter_seq=seq,
                    outline_node_id=node_id, provider=llm,
                )
                review = result.model_dump()
                issues = review.get("issues", [])
                warnings = list(review.get("warnings", []))
                for issue in issues:
                    reason = issue.get("reason") or issue.get("message") or "发现剧情约束偏差"
                    excerpt = issue.get("excerpt", "")
                    warnings.append(f"《{node_title}》正文已保存，剧情复核：{reason}"
                                    + (f"；原文：『{excerpt}』" if excerpt else ""))
                incomplete = not review.get("review_complete", False) or review.get("truncated", False)
                if incomplete and not warnings:
                    reason = "正文、指令或设定超过复核上限，检查输入已截断" if review.get("truncated") else "剧情复核结果未完整核验"
                    warnings.append(f"《{node_title}》正文已保存，{reason}，请核对本章后再继续。")
                warnings = list(dict.fromkeys(warnings))
                quality_needs_review = quality_needs_review or bool(issues) or incomplete
                yield _sse({
                    "type": "story_check", "chapter_id": chapter_id, "seq": seq,
                    "node_id": node_id, "title": node_title, "issues": issues,
                    "warnings": warnings, "truncated": bool(review.get("truncated", False)),
                    "history_truncated": bool(review.get("history_truncated", False)),
                    "previous_chapter_title": review.get("previous_chapter_title", ""),
                    "chapter_title": review.get("chapter_title", node_title),
                    "review_complete": bool(review.get("review_complete", False)),
                    "message": "正文已保存，剧情复核需要人工核对" if quality_needs_review else "剧情复核完成，未发现明显偏差",
                })
            except Exception:  # noqa: BLE001  质量服务失败不得撤销正文，也不输出私有网关。
                db.rollback()
                quality_needs_review = True
                message = f"《{node_title}》正文已保存，剧情复核未能完成，请核对本章后再继续。"
                yield _sse({
                    "type": "error", "scope": "quality", "fatal": False,
                    "code": "quality_check_failed", "kind": "story_check", "saved": True,
                    "message": message, "seq": seq, "chapter_id": chapter_id,
                    "node_id": node_id, "title": node_title,
                })
                yield _sse({
                    "type": "story_check", "chapter_id": chapter_id, "seq": seq,
                    "node_id": node_id, "title": node_title, "issues": [], "warnings": [message],
                    "truncated": False, "review_complete": False, "message": message,
                })
            has_quality_warning = has_quality_warning or quality_needs_review
            if quality_needs_review and any(
                not _has_chapter(db, novel_id, upcoming.id) for upcoming in nodes[node_index + 1:]
            ):
                state.pause_message = f"《{node_title}》正文已保存，检测到规则或剧情问题、或复核未完成，已暂停；请核对后再恢复。"
                state.paused = True

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
        try:
            if journal is not None:
                try:
                    journal.close()
                except Exception as exc:
                    cleanup_error = exc
            if db is not None:
                try:
                    db.close()
                except Exception as exc:  # noqa: BLE001
                    cleanup_error = exc
        finally:
            # 缓存收尾之前仍由本次任务持有，避免恢复页提前删除其 active 记录。
            registry.finish(novel_id, state.owner_id)

    if task_error is not None:
        yield _sse(task_error)
    if cleanup_error is not None:
        terminal_status = "incomplete"
        bodies_saved = task_error is None and generated + skipped == total
        yield _sse({
            "type": "error", "scope": "task", "fatal": True,
            "code": "autopilot_cleanup_failed", "saved": bodies_saved,
            "message": f"正文均已保存，自动驾驶收尾失败（恢复缓存或会话清理）：{cleanup_error}"
            if bodies_saved else f"自动驾驶会话关闭失败：{cleanup_error}",
            **counts(),
        })
    message = {
        "completed": "自动驾驶完成",
        "incomplete": "自动驾驶结束，仍有未完成章节",
        "stopped": "已停止",
    }[terminal_status]
    if terminal_reason == "failure_threshold":
        message = "连续三次生成失败，自动驾驶已结束，仍有未完成章节"
    elif terminal_status == "completed" and has_quality_warning:
        message = "自动驾驶完成，正文已保存；规则或剧情复核存在警告，请核对。"
    elif cleanup_error is not None and task_error is None and generated + skipped == total:
        message = "正文均已保存，恢复缓存或会话清理未完成，请核对章节。"
    yield _sse({
        "type": "done", "status": terminal_status, "message": message,
        "reason": terminal_reason, **counts(),
    })
