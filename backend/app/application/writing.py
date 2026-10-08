"""写作用例：write_chapter / continue_chapter / rewrite_passage（均 SSE 流式）。"""
import json

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.application.context import (
    build_context, chapter_excerpt, prior_chapters, prior_chapter_context, recent_history,
)
from app.application.revisions import (
    AUTHOR_FIELDS,
    SOURCE_CONTINUE,
    SOURCE_REWRITE,
    update_chapter_with_revision,
)
from app.application import generated_chapter, generated_consistency
from app.infrastructure.ai.llm_provider import LLMProvider
from app.infrastructure.ai.prompts import (
    CONTINUE_SYSTEM,
    NEXT_CHAPTER_SYSTEM,
    NEXT_CHAPTER_USER,
    REWRITE_INTENTS,
    REWRITE_SYSTEM,
    WRITE_SYSTEM,
)
from app.infrastructure.repository import ChapterRepo, OutlineRepo
from app.infrastructure import models
from app.interfaces import schemas

llm = LLMProvider()


class RewriteSelectionError(ValueError):
    code = "chapter_rewrite_selection_invalid"


def sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _stream_error(exc: Exception) -> str:
    if getattr(exc, "status_code", None) == 409:
        value = {
            "type": "error", "message": getattr(exc, "detail", str(exc)),
            "code": "chapter_write_conflict", "status": 409,
        }
        if isinstance(exc, generated_chapter.GeneratedChapterConflict):
            value.update(hint=exc.hint, saved=exc.saved)
        return sse(value)
    return sse({
        "type": "error", "message": str(exc),
        "code": getattr(exc, "code", "generation_failed"),
    })


def _generation_storage_error() -> str:
    return sse({"type": "error", "code": "generation_failed", "status": 500,
        "message": "章节保存或读取未完成，请保留候选并核对实际目录",
        "hint": "回复是否保存尚未确认；请复制已输出正文并核对实际章节，不会自动重试"})


def _chapter_dict(chapter) -> dict:
    return schemas.ChapterRead.model_validate(chapter).model_dump()


def _chapter_context(content: str, max_chars: int = 7000) -> str:
    """保留上一章章首与结尾，兼顾身份建立和结尾钩子。"""
    return chapter_excerpt(content, max_chars)


def _recent_history(chapters: list, previous) -> str:
    """给出上一章之前最多两章的短脉络，未定稿章节用首尾片段降级。"""
    return recent_history(chapters, previous)


def _outline_contract(outline) -> str:
    """把本章大纲放在正文前的临近位置，避免被长前情冲淡。"""
    if outline is None:
        return ""
    summary = (outline.summary or "").strip()
    return (
        "【本章大纲目标（尚未发生，必须推进）】\n"
        f"标题：{outline.title}\n"
        f"概要：{summary or '未填写'}\n"
        "概要中的具体人物、物件、气味、数字、地点及因果线索不可随意替换。"
        "Story Bible明确事实优先，未定稿前情不得覆盖设定；"
        "没有设定冲突时，用合理过渡承接前章并推进大纲目标。"
    )


def write_chapter(
    db: Session,
    novel_id: str,
    directive: str = "",
    title: str = "",
    outline_node_id: str = "",
    previous_chapter_id: str = "",
    action_message_id: str = "",
    expected_latest_id: str | None = None,
):
    """新建一章：可承接指定上一章结尾，流式输出并落库为 draft。"""
    try:
        generated_chapter.require_generation_session(db)
        repo = ChapterRepo(db)
        if action_message_id:
            existing = generated_chapter.existing_chat_chapter(db, novel_id, action_message_id)
            if existing is not None:
                chapter = _chapter_dict(existing)
                yield sse({"type": "done", "chapter": chapter, "consistency": generated_consistency.result(
                    None, novel_id, chapter["id"], chapter["content"], "existing_action")})
                return
        source = generated_chapter.capture_source(db, novel_id)
        if expected_latest_id is not None and source.latest_id != expected_latest_id:
            raise generated_chapter.GeneratedChapterConflict()
        # 干净会话中的旧ORM缓存不是本次来源；不丢弃任何调用方未提交修改。
        db.expire_all()
        seq = source.next_seq
        position = f"第 {seq} 章" + (f"《{title}》" if title else "")
        outline = None
        if outline_node_id:
            outline = OutlineRepo(db).get(novel_id, outline_node_id)
            if outline.level != "chapter":
                raise ValueError("只能绑定章级大纲节点")
            position += f"\n章级大纲：《{outline.title}》"
            if outline.summary:
                position += f"\n本章剧情要点：{outline.summary}"
        outline_contract = _outline_contract(outline)
        context = build_context(db, novel_id, directive, position,
                                chapter_seq=seq, outline_node_id=outline_node_id)
        system_prompt = WRITE_SYSTEM
        user_prompt = f"{context}\n\n{outline_contract}\n\n请开始撰写本章正文："
        chapters = prior_chapters(db, novel_id, seq, outline_node_id)
        previous = chapters[-1] if chapters else None
        if previous_chapter_id:
            previous = repo.get(novel_id, previous_chapter_id)
            all_chapters = repo.list(novel_id)
            latest = all_chapters[-1] if all_chapters else None
            if latest is None or previous.id != latest.id:
                raise ValueError("只能从最新章节生成下一章，请先选择章节列表中的最后一章")
            if previous.id not in {chapter.id for chapter in chapters}:
                raise ValueError("所选上一章不在本章大纲之前，请先核对章节与大纲顺序")
        previous_source = generated_chapter.capture_previous_source(previous) if previous is not None else None
        if previous_source is not None:
            system_prompt = NEXT_CHAPTER_SYSTEM
            user_prompt = NEXT_CHAPTER_USER.format(
                context=context,
                recent_history=_recent_history(chapters, previous),
                previous_title=previous_source.title or f"第{previous.seq}章",
                previous_summary=(previous.summary or "未沉淀，以正文为准"),
                previous_content=_chapter_context(previous_source.content),
                outline_contract=outline_contract,
            )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        full = ""
        for chunk in llm.stream_complete(messages, max_tokens=get_settings().writing_max_tokens):
            full += chunk
            yield sse({"type": "token", "content": chunk})

        if not full.strip():
            raise ValueError("未生成有效正文，请重试")

        data = dict(
            seq=seq, title=title or f"第{seq}章", content=full,
            status="draft", directive=directive, outline_node_id=outline_node_id,
        )
        facts = generated_consistency.capture_rule_facts(db, novel_id)
        chapter = generated_chapter.save_generated_chapter(
            db, novel_id, source, data, action_message_id=action_message_id,
            previous_source=previous_source,
        )
        chapter = _chapter_dict(chapter)
        yield sse({"type": "done", "chapter": chapter, "consistency": generated_consistency.result(
            facts, novel_id, chapter["id"], chapter["content"], "saved_chapter")})
    except SQLAlchemyError:
        # 写事务只由save helper负责回滚；不把SQL、参数或原文带进作者错误提示。
        yield _generation_storage_error()
    except Exception as exc:  # noqa: BLE001
        yield _stream_error(exc)


def continue_chapter(
    db: Session,
    novel_id: str,
    chapter_id: str,
    directive: str = "",
):
    """续写当前章：携带上文 + 设定，流式追加到正文。"""
    try:
        repo = ChapterRepo(db)
        chapter = repo.get(novel_id, chapter_id)
        source_fields = {field: getattr(chapter, field) for field in AUTHOR_FIELDS}
        position = f"第 {chapter.seq} 章《{chapter.title}》续写"
        context = build_context(db, novel_id, directive, position,
                                chapter_seq=chapter.seq, outline_node_id=chapter.outline_node_id)
        previous_context = prior_chapter_context(db, novel_id, chapter.seq, chapter.outline_node_id).text
        tail = chapter.content[-1500:]
        messages = [
            {"role": "system", "content": CONTINUE_SYSTEM},
            {
                "role": "user",
                "content": f"{context}\n\n{previous_context}\n\n【本章已写内容结尾】\n{tail}\n\n请自然续写：",
            },
        ]

        full = ""
        for chunk in llm.stream_complete(messages):
            full += chunk
            yield sse({"type": "token", "content": chunk})

        if not full.strip():
            raise ValueError("未生成有效续写内容，请重试")

        # 追加正文属于覆盖既有作者内容：先留旧版本，再与追加同一事务提交
        facts = generated_consistency.capture_rule_facts(db, novel_id)
        updated = update_chapter_with_revision(
            db,
            novel_id,
            chapter_id,
            {"content": source_fields["content"] + full},
            SOURCE_CONTINUE,
            expected_author_fields=source_fields,
        )
        chapter = _chapter_dict(updated)
        yield sse({"type": "done", "chapter": chapter, "consistency": generated_consistency.result(
            facts, novel_id, chapter["id"], chapter["content"], "saved_chapter")})
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        yield _stream_error(exc)


def rewrite_passage(
    db: Session,
    novel_id: str,
    chapter_id: str,
    passage: str,
    intent: str = "",
    preview: bool = False,
    *, expected_author_fields: dict | None = None,
    selection_start: int | None = None,
    selection_end: int | None = None,
):
    """改写指定段落：流式输出改写结果。

    preview 为 True 时只返回改写结果与拼好的整章内容，不写库；
    默认 False 保持原行为，直接替换原文对应段落并落库。
    """
    try:
        # 真正数据库列投影绕过旧ORM缓存，也不刷新/flush调用方的未提交作者字段。
        with db.no_autoflush:
            table = models.Chapter.__table__
            row = db.execute(select(table).where(table.c.id == chapter_id, table.c.novel_id == novel_id)).mappings().first()
            if row is None:
                raise HTTPException(status_code=404, detail="章节不存在")
            chapter = dict(row)
            source_fields = {field: chapter[field] for field in AUTHOR_FIELDS}
            if expected_author_fields is not None and any(
                source_fields[field] != expected_author_fields[field] for field in AUTHOR_FIELDS
            ):
                raise HTTPException(status_code=409, detail="原稿已变化，请重新读取核对后生成改写；当前稿未被覆盖")
            original = source_fields["content"]
            if selection_start is None and selection_end is None:
                selection_start = original.find(passage)
                if preview and selection_start >= 0 and original.find(passage, selection_start + 1) >= 0:
                    raise RewriteSelectionError("原文有多处相同段落，请重新选择精确范围，不能猜测第一处")
                selection_end = selection_start + len(passage)
            if (type(selection_start) is not int or type(selection_end) is not int
                    or not 0 <= selection_start < selection_end <= len(original)
                    or original[selection_start:selection_end] != passage):
                raise RewriteSelectionError("选区与原稿不一致，请重新选择完整原文；原稿未改动")
            position = f"第 {chapter['seq']} 章《{chapter['title']}》改写"
            context = build_context(db, novel_id, "", position,
                                    chapter_seq=chapter["seq"], outline_node_id=chapter["outline_node_id"])
            previous_context = prior_chapter_context(db, novel_id, chapter["seq"], chapter["outline_node_id"]).text
        intent_text = REWRITE_INTENTS.get(intent, REWRITE_INTENTS[""])
        messages = [
            {"role": "system", "content": REWRITE_SYSTEM},
            {
                "role": "user",
                "content": f"{context}\n\n{previous_context}\n\n【改写意图】{intent_text}\n【待改写段落】\n{passage}\n\n请输出改写后的段落：",
            },
        ]

        full = ""
        for chunk in llm.stream_complete(messages):
            full += chunk
            yield sse({"type": "token", "content": chunk})

        if not full.strip():
            raise ValueError("未生成有效改写内容，请重试")

        new_content = original[:selection_start] + full + original[selection_end:]
        facts = generated_consistency.capture_rule_facts(db, novel_id)
        if preview:
            yield sse(
                {
                    "type": "done",
                    "preview": True,
                    "content": new_content,
                    "chapter": _chapter_dict(chapter),
                    "expected_author_fields": source_fields,
                    "selection_start": selection_start,
                    "selection_end": selection_end,
                    "consistency": generated_consistency.result(
                        facts, novel_id, chapter_id, new_content, "preview_candidate"),
                }
            )
            return

        # 替换正文属于覆盖既有作者内容：先留旧版本，再与替换同一事务提交
        updated = update_chapter_with_revision(
            db, novel_id, chapter_id, {"content": new_content}, SOURCE_REWRITE,
            expected_author_fields=source_fields,
        )
        chapter = _chapter_dict(updated)
        yield sse({"type": "done", "chapter": chapter, "consistency": generated_consistency.result(
            facts, novel_id, chapter["id"], chapter["content"], "saved_chapter")})
    except Exception as exc:  # noqa: BLE001
        if not preview:
            db.rollback()
        yield _stream_error(exc)
