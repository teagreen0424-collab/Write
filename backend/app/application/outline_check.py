"""按需检查章节与所绑定章级大纲的契合度；不修改正文或数据库。"""

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.application.consistency import _parse_json
from app.infrastructure.ai.llm_provider import LLMProvider
from app.infrastructure.ai.prompts import OUTLINE_CHECK_SYSTEM, OUTLINE_CHECK_USER
from app.infrastructure.repository import ChapterRepo, OutlineRepo
from app.interfaces.schemas import OutlineCheckResult

llm = LLMProvider()

MAX_CONTENT_CHARS = 10000


def _review_excerpt(content: str) -> tuple[str, bool]:
    if len(content) <= MAX_CONTENT_CHARS:
        return content, False
    return content[:6000] + "\n…（正文中段已省略）…\n" + content[-4000:], True


def check_chapter_outline(
    db: Session, novel_id: str, chapter_id: str, content: str | None = None,
) -> OutlineCheckResult:
    """一次模型调用检查当前正文；传入 content 时可检查尚未保存的编辑稿。"""
    chapter = ChapterRepo(db).get(novel_id, chapter_id)
    if not chapter.outline_node_id:
        raise HTTPException(status_code=400, detail="本章未绑定章级大纲")
    outline = OutlineRepo(db).get(novel_id, chapter.outline_node_id)
    if outline.level != "chapter" or not (outline.summary or "").strip():
        raise HTTPException(status_code=400, detail="章级大纲缺少概要，暂无法检查契合度")
    body = chapter.content if content is None else content
    if not body.strip():
        raise HTTPException(status_code=400, detail="正文为空，无法检查大纲契合度")

    excerpt, truncated = _review_excerpt(body)
    raw = llm.complete(
        [
            {"role": "system", "content": OUTLINE_CHECK_SYSTEM},
            {"role": "user", "content": OUTLINE_CHECK_USER.format(
                title=outline.title, summary=outline.summary, content=excerpt,
            )},
        ],
        temperature=0.1,
        max_tokens=1500,
    )
    try:
        parsed = _parse_json(raw)
        if not isinstance(parsed, dict):
            raise ValueError("模型结果不是 JSON 对象")
        issues = parsed.get("issues")
        if not isinstance(issues, list):
            raise ValueError("模型结果缺少问题列表")
        result = OutlineCheckResult.model_validate({
            "issues": issues[:6],
            "truncated": truncated,
            "outline_title": outline.title,
        })
    except (ValueError, TypeError, ValidationError) as exc:
        raise HTTPException(status_code=502, detail="模型返回的大纲检查格式无效，请重试") from exc

    # 不把模型编造的引文当成正文证据展示。
    for issue in result.issues:
        if issue.excerpt and issue.excerpt not in body:
            issue.excerpt = ""
    return result
