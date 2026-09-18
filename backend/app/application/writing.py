"""写作用例：write_chapter / continue_chapter / rewrite_passage（均 SSE 流式）。"""
import json

from sqlalchemy.orm import Session

from app.application.context import build_context
from app.infrastructure.ai.llm_provider import LLMProvider
from app.infrastructure.ai.prompts import (
    CONTINUE_SYSTEM,
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


def write_chapter(
    db: Session,
    novel_id: str,
    directive: str = "",
    title: str = "",
    outline_node_id: str = "",
):
    """新建一章：生成 seq/title/content，流式输出，落库为 draft。"""
    try:
        repo = ChapterRepo(db)
        seq = repo.next_seq(novel_id)
        position = f"第 {seq} 章" + (f"《{title}》" if title else "")
        context = build_context(db, novel_id, directive, position)
        messages = [
            {"role": "system", "content": WRITE_SYSTEM},
            {"role": "user", "content": f"{context}\n\n请开始撰写本章正文："},
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
