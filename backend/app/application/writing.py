"""写作用例：write_chapter / continue_chapter / rewrite_passage（均 SSE 流式）。"""
import json

from sqlalchemy.orm import Session

from app.application.context import build_context
from app.infrastructure.ai.llm_provider import LLMProvider
from app.infrastructure.ai.prompts import (
    CONTINUE_SYSTEM,
    NEXT_CHAPTER_SYSTEM,
    NEXT_CHAPTER_USER,
    REWRITE_INTENTS,
    REWRITE_SYSTEM,
    WRITE_SYSTEM,
)
from app.infrastructure.repository import ChapterRepo
from app.interfaces import schemas

llm = LLMProvider()


def sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _chapter_dict(chapter) -> dict:
    return schemas.ChapterRead.model_validate(chapter).model_dump()


def _chapter_context(content: str, max_chars: int = 7000) -> str:
    """保留上一章章首与结尾，兼顾身份建立和结尾钩子。"""
    if len(content) <= max_chars:
        return content
    head_chars = 3000
    tail_chars = max_chars - head_chars
    return content[:head_chars] + "\n\n…（上一章中段已省略）…\n\n" + content[-tail_chars:]


def _recent_history(chapters: list, previous) -> str:
    """给出上一章之前最多两章的短脉络，未定稿章节用首尾片段降级。"""
    earlier = [chapter for chapter in chapters if chapter.seq < previous.seq][-2:]
    if not earlier:
        return "无（当前从第一章继续）"
    lines: list[str] = []
    for chapter in earlier:
        brief = (chapter.summary or "").strip()
        if not brief:
            compact = (chapter.content or "").strip()
            if len(compact) > 600:
                compact = compact[:300] + "…" + compact[-300:]
            brief = compact or "无摘要"
        lines.append(f"第{chapter.seq}章《{chapter.title}》：{brief}")
    return "\n".join(lines)


def write_chapter(
    db: Session,
    novel_id: str,
    directive: str = "",
    title: str = "",
    outline_node_id: str = "",
    previous_chapter_id: str = "",
):
    """新建一章：可承接指定上一章结尾，流式输出并落库为 draft。"""
    try:
        repo = ChapterRepo(db)
        seq = repo.next_seq(novel_id)
        position = f"第 {seq} 章" + (f"《{title}》" if title else "")
        context = build_context(db, novel_id, directive, position)
        system_prompt = WRITE_SYSTEM
        user_prompt = f"{context}\n\n请开始撰写本章正文："
        if previous_chapter_id:
            previous = repo.get(novel_id, previous_chapter_id)
            chapters = repo.list(novel_id)
            latest = chapters[-1] if chapters else None
            if latest is None or previous.id != latest.id:
                raise ValueError("只能从最新章节生成下一章，请先选择章节列表中的最后一章")
            system_prompt = NEXT_CHAPTER_SYSTEM
            user_prompt = NEXT_CHAPTER_USER.format(
                context=context,
                recent_history=_recent_history(chapters, previous),
                previous_title=previous.title or f"第{previous.seq}章",
                previous_summary=(previous.summary or "未沉淀，以正文为准"),
                previous_content=_chapter_context(previous.content),
            )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        full = ""
        for chunk in llm.stream_complete(messages):
            full += chunk
            yield sse({"type": "token", "content": chunk})

        chapter = repo.create(
            novel_id,
            seq=seq,
            title=title or f"第{seq}章",
            content=full,
            status="draft",
            directive=directive,
            outline_node_id=outline_node_id,
        )
        yield sse({"type": "done", "chapter": _chapter_dict(chapter)})
    except Exception as exc:  # noqa: BLE001
        yield sse({"type": "error", "message": str(exc)})


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
        position = f"第 {chapter.seq} 章《{chapter.title}》续写"
        context = build_context(db, novel_id, directive, position)
        tail = chapter.content[-1500:]
        messages = [
            {"role": "system", "content": CONTINUE_SYSTEM},
            {
                "role": "user",
                "content": f"{context}\n\n【本章已写内容结尾】\n{tail}\n\n请自然续写：",
            },
        ]

        full = ""
        for chunk in llm.stream_complete(messages):
            full += chunk
            yield sse({"type": "token", "content": chunk})

        repo.update(novel_id, chapter_id, content=chapter.content + full)
        updated = repo.get(novel_id, chapter_id)
        yield sse({"type": "done", "chapter": _chapter_dict(updated)})
    except Exception as exc:  # noqa: BLE001
        yield sse({"type": "error", "message": str(exc)})


def rewrite_passage(
    db: Session,
    novel_id: str,
    chapter_id: str,
    passage: str,
    intent: str = "",
    preview: bool = False,
):
    """改写指定段落：流式输出改写结果。

    preview 为 True 时只返回改写结果与拼好的整章内容，不写库；
    默认 False 保持原行为，直接替换原文对应段落并落库。
    """
    try:
        repo = ChapterRepo(db)
        chapter = repo.get(novel_id, chapter_id)
        if passage not in chapter.content:
            raise ValueError("未在正文中找到待改写段落，请确认已选中原文")

        position = f"第 {chapter.seq} 章《{chapter.title}》改写"
        context = build_context(db, novel_id, "", position)
        intent_text = REWRITE_INTENTS.get(intent, REWRITE_INTENTS[""])
        messages = [
            {"role": "system", "content": REWRITE_SYSTEM},
            {
                "role": "user",
                "content": f"{context}\n\n【改写意图】{intent_text}\n【待改写段落】\n{passage}\n\n请输出改写后的段落：",
            },
        ]

        full = ""
        for chunk in llm.stream_complete(messages):
            full += chunk
            yield sse({"type": "token", "content": chunk})

        new_content = chapter.content.replace(passage, full, 1)
        if preview:
            yield sse(
                {
                    "type": "done",
                    "preview": True,
                    "content": new_content,
                    "chapter": _chapter_dict(chapter),
                }
            )
            return

        repo.update(novel_id, chapter_id, content=new_content)
        updated = repo.get(novel_id, chapter_id)
        yield sse({"type": "done", "chapter": _chapter_dict(updated)})
    except Exception as exc:  # noqa: BLE001
        yield sse({"type": "error", "message": str(exc)})
