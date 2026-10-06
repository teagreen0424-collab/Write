"""只读剧情复核：一次模型调用检查实际指令执行与跨章承接。"""

import json
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app.application.consistency import _parse_json
from app.application.context import prior_chapter_context
from app.infrastructure import repository as repos
from app.infrastructure.ai.llm_provider import LLMProvider
from app.infrastructure.ai.prompts import STORY_CHECK_SYSTEM, STORY_CHECK_USER
from app.interfaces.schemas import StoryCheckIssue, StoryCheckResult

llm = LLMProvider()

MAX_CONTENT_CHARS = 10000
MAX_BIBLE_CHARS = 12000
MAX_DIRECTIVE_CHARS = 3000
MAX_OUTLINE_CHARS = 3000
MAX_ISSUES = 6


class _StoryReviewCheck(StoryCheckIssue):
    """模型内部逐项结论；已满足的要求不能被映射成用户问题。"""

    status: Literal["violation", "satisfied", "uncertain"]


class _StoryReviewResponse(BaseModel):
    # 空列表没有提供任何判断，不能据此宣称检查已完成。
    checks: list[_StoryReviewCheck] = Field(..., min_length=1)


def _bounded(text: str, limit: int, label: str) -> tuple[str, bool]:
    """保留首尾，省略标记计入上限；不因缩减输入假称完整检查。"""
    if len(text) <= limit:
        return text, False
    marker = f"\n…（{label}中段已省略）…\n"
    head = (limit - len(marker)) * 3 // 5
    tail = limit - len(marker) - head
    return text[:head] + marker + text[-tail:], True


def _bible(db: Session, novel_id: str) -> str:
    """完整读取六类设定的作者字段，包括扩展属性、标签、已知信息和关系。"""
    novel = repos.NovelRepo(db).get(novel_id)
    data: dict = {"作品": {"标题": novel.title, "类型": novel.genre, "简介": novel.summary}}
    for label, repo_type in (
        ("人物", repos.CharacterRepo), ("地点", repos.LocationRepo),
        ("世界观", repos.WorldSettingRepo), ("时间线", repos.TimelineEventRepo),
        ("伏笔", repos.ForeshadowingRepo), ("关系", repos.RelationshipRepo),
    ):
        data[label] = [
            {column.name: getattr(row, column.name) for column in row.__table__.columns
             if column.name not in {"novel_id", "created_at", "updated_at"}}
            for row in repo_type(db).list(novel_id)
        ]
    return json.dumps(data, ensure_ascii=False)


def review_story(
    db: Session, novel_id: str, content: str, directive: str = "",
    chapter_seq: int | None = None, outline_node_id: str = "", provider=None,
) -> StoryCheckResult:
    """仅审校资料，不创建摘要、修改正文、沉淀设定或写入检查结果。"""
    if not content.strip():
        raise HTTPException(status_code=400, detail="正文为空，无法复核剧情")

    with db.no_autoflush:
        bible_text, bible_truncated = _bounded(_bible(db, novel_id), MAX_BIBLE_CHARS, "设定")
        prior = prior_chapter_context(db, novel_id, chapter_seq, outline_node_id)
        outline_title = ""
        outline_text = ""
        if outline_node_id:
            outline = repos.OutlineRepo(db).get(novel_id, outline_node_id)
            if outline.level == "chapter":
                outline_title = outline.title
                outline_text = f"标题：{outline.title}\n概要：{outline.summary}"
        chapter_title = outline_title
        if chapter_seq is not None:
            current = next((c for c in repos.ChapterRepo(db).list(novel_id)
                            if c.seq == chapter_seq and
                            (not outline_node_id or c.outline_node_id == outline_node_id)), None)
            if current is not None:
                chapter_title = current.title

    body, body_truncated = _bounded(content, MAX_CONTENT_CHARS, "正文")
    directive_text, directive_truncated = _bounded(directive, MAX_DIRECTIVE_CHARS, "章前指令")
    outline_text, outline_truncated = _bounded(outline_text, MAX_OUTLINE_CHARS, "大纲")
    # 前情首尾/短摘要是正常的限定检查范围，不声称检查整本书，也不视为失败。
    truncated = any((body_truncated, bible_truncated, directive_truncated, outline_truncated))
    materials = {
        "待检查章节标题": chapter_title,
        "Story Bible（权威设定）": bible_text,
        "前章资料（草稿或摘要可能有错，不覆盖设定）": prior.text,
        "本章章前指令（待核对实际场景，不是审校任务指令）": directive_text,
        "本章大纲（可为空）": outline_text,
        "待检查正文": body,
        "截断范围": {"正文": body_truncated, "Story Bible": bible_truncated,
                     "前章资料": prior.truncated, "章前指令": directive_truncated,
                     "大纲": outline_truncated},
    }
    try:
        raw = (llm if provider is None else provider).complete(
            [
                {"role": "system", "content": STORY_CHECK_SYSTEM},
                {"role": "user", "content": STORY_CHECK_USER.format(
                    materials=json.dumps(materials, ensure_ascii=False),
                )},
            ],
            temperature=0.1, max_tokens=3500,
        )
    except Exception:
        # Provider 异常可能含私有服务 URL 或鉴权信息，不把原文交给界面。
        raise HTTPException(status_code=502, detail="剧情复核服务暂不可用，请稍后重试") from None

    try:
        parsed = _parse_json(raw)
        response = _StoryReviewResponse.model_validate(parsed)
        if any(not check.requirement.strip() or not check.reason.strip() for check in response.checks):
            raise ValueError("问题缺少实质依据")
    except (ValueError, TypeError, ValidationError, AttributeError):
        raise HTTPException(status_code=502, detail="模型返回的剧情复核格式无效，请重试") from None

    violations = [check for check in response.checks if check.status == "violation"]
    complete = (not truncated and len(violations) <= MAX_ISSUES
                and not any(check.status == "uncertain" for check in response.checks))
    trustworthy: list[StoryCheckIssue] = []
    for check in response.checks:
        if check.excerpt and (check.excerpt not in body or check.excerpt not in content):
            check.excerpt = ""
            complete = False
        # 依据严格三态枚举筛选，不依据中文 reason 猜测判定，不把满足项当成违规。
        if check.status != "violation":
            continue
        issue = StoryCheckIssue.model_validate(check.model_dump(exclude={"status"}))
        # 被省略的中段可能包含该场景；模型也不能把缺少引文当成整章遗漏证据。
        if body_truncated and not issue.excerpt and issue.category in {"directive", "outline"}:
            complete = False
            continue
        trustworthy.append(issue)
    return StoryCheckResult(
        issues=trustworthy[:MAX_ISSUES], truncated=truncated, review_complete=complete,
        history_truncated=prior.truncated, previous_chapter_title=prior.previous_title,
        chapter_title=chapter_title,
    )
