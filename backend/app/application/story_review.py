"""只读剧情复核：一次模型调用检查实际指令执行与跨章承接。"""

import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app.application.consistency import _parse_json
from app.application.context import prior_chapter_context
from app.infrastructure import repository as repos
from app.infrastructure.ai.llm_provider import LLMProvider
from app.infrastructure.ai.prompts import STORY_CHECK_SYSTEM, STORY_CHECK_USER
from app.interfaces.schemas import (
    StoryCheckIssue, StoryCheckResult, StoryRequirement, StoryRequirementCheck,
)

llm = LLMProvider()

MAX_CONTENT_CHARS = 10000
MAX_BIBLE_CHARS = 12000
MAX_DIRECTIVE_CHARS = 3000
MAX_OUTLINE_CHARS = 3000
MAX_ISSUES = 6
MAX_PLAN_CHARS = 12000


@dataclass(frozen=True)
class PlannedRequirement:
    """按作者原文构造核对项；同句上下文不交给模型重新编造。"""

    id: str
    source: Literal["directive", "outline"]
    text: str
    context: str
    evidence_required: bool = True


def _split_author_text(text: str, separators: str) -> list[str]:
    """只切引号/括号之外的分隔符，保留数字分组逗号与引用内动作。"""
    pairs = {"“": "”", "‘": "’", '"': '"', "'": "'", "（": "）", "(": ")",
             "[": "]", "【": "】", "《": "》"}
    stack: list[str] = []
    chunks: list[str] = []
    start = 0
    for index, char in enumerate(text):
        if stack and char == stack[-1]:
            stack.pop()
            continue
        if char in pairs:
            # 英文词内撇号不是引用开头。
            if char == "'" and index and index + 1 < len(text) and all(
                letter.isascii() and letter.isalpha() for letter in (text[index - 1], text[index + 1])
            ):
                continue
            stack.append(pairs[char])
            continue
        if stack or char not in separators:
            continue
        if char == "," and index and index + 1 < len(text) and text[index - 1].isdigit() and text[index + 1].isdigit():
            continue
        value = text[start:index].strip()
        if value:
            chunks.append(value)
        start = index + 1
    value = text[start:].strip()
    if value:
        chunks.append(value)
    return chunks


def _is_output_metadata(text: str) -> bool:
    """仅明确篇幅/输出格式元要求不需剧情引文；不解析或设置字数硬界。"""
    text = re.sub(r"\s+", "", text)
    number = r"(?:[0-9]+(?:,[0-9]{3})*|[零一二三四五六七八九十百千两]+)"
    amount = number + r"(?:\s*(?:至|到|[-—–~～])\s*" + number + r")?"
    count_pattern = (
        r"(?:(?:每|本|此)章)?(?:正文|文字|字数|篇幅)(?:字数|篇幅)?"
        r"(?:约|应|须|需|要求|控制在|保持在|至少|至多|不超过|不少于|为|限定为|在)*"
        + amount + r"(?:个)?(?:中文)?(?:字|字符)?(?:以内|左右|上下|之间)?(?:[（(][^）)]*[）)])?"
    )
    if re.fullmatch(count_pattern, text):
        return True
    return bool(re.fullmatch(
        r"(?:直接|只|仅)?(?:写|输出)(?:场景|动作|对话|正文|正文本身|故事正文)(?:(?:、|与|和|及)(?:场景|动作|对话))*"
        r"|(?:不要|不|不得)(?:列|列出|写|作|做|输出|添加)(?:小节|章节总结|小节标题|标题|提纲|解释|额外说明)",
        text,
    ))


def _is_chapter_scope(text: str) -> bool:
    """只有单纯作用范围元指令可不作为正文行为；带任何情节条件则保留。"""
    return bool(re.fullmatch(
        r"(?:只|仅)(?:影响|作用于|对)(?:本章|这一章|当前章|接下来这一章)(?:生效|有效)?",
        text,
    ))


def build_requirement_plan(directive: str = "", outline: str = "") -> list[PlannedRequirement]:
    """程序生成稳定编号清单，切逗号分开动作并携带完整原句限定。"""
    plan: list[PlannedRequirement] = []
    for source, raw, prefix in (("directive", directive, "d"), ("outline", outline, "o")):
        index = 0
        for sentence in _split_author_text(raw, "。\n\r"):
            for clause in _split_author_text(sentence, "；;，,"):
                if _is_chapter_scope(clause):
                    continue
                index += 1
                plan.append(PlannedRequirement(
                    id=f"{prefix}{index:03}", source=source, text=clause, context=sentence,
                    evidence_required=not _is_output_metadata(clause),
                ))
    return plan


def _plan_materials(plan: list[PlannedRequirement]) -> str:
    """每个原句只传一次，避免同句动作分开后重复长上下文。"""
    contexts: dict[str, str] = {}
    context_ids: dict[tuple[str, str], str] = {}
    requirements: list[dict] = []
    for item in plan:
        key = (item.source, item.context)
        if key not in context_ids:
            context_id = f"c{len(context_ids) + 1:03}"
            context_ids[key] = context_id
            contexts[context_id] = item.context
        requirements.append({"id": item.id, "source": item.source, "text": item.text,
                             "context_id": context_ids[key], "evidence_required": item.evidence_required})
    return json.dumps({"requirements": requirements, "contexts": contexts}, ensure_ascii=False)


class _StoryReviewCheck(StoryCheckIssue):
    """模型内部逐项结论；已满足的要求不能被映射成用户问题。"""

    status: Literal["violation", "satisfied", "uncertain"]


class _PlannedRequirementCheck(StoryRequirementCheck):
    """来源由计划确定；模型给错来源不能偷偷改成另一类要求。"""

    category: str | None = None
    severity: Literal["high", "medium", "low"] = "medium"
    excerpt: str = Field(default="", max_length=400)
    reason: str = Field(..., min_length=1, max_length=1200)


class _StoryReviewResponse(BaseModel):
    requirement_checks: list[_PlannedRequirementCheck] = Field(default_factory=list)
    checks: list[_StoryReviewCheck] = Field(default_factory=list)


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
        outline_summary = ""
        if outline_node_id:
            outline = repos.OutlineRepo(db).get(novel_id, outline_node_id)
            if outline.level == "chapter":
                outline_title = outline.title
                outline_summary = outline.summary
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
    plan = build_requirement_plan(directive, outline_summary)
    plan_text, plan_truncated = _bounded(_plan_materials(plan), MAX_PLAN_CHARS, "核对计划")
    # 前情首尾/短摘要是正常的限定检查范围，不声称检查整本书，也不视为失败。
    truncated = any((body_truncated, bible_truncated, directive_truncated, outline_truncated, plan_truncated))
    materials = {
        "待检查章节标题": chapter_title,
        "Story Bible（权威设定）": bible_text,
        "前章资料（草稿或摘要可能有错，不覆盖设定）": prior.text,
        "本章章前指令（待核对实际场景，不是审校任务指令）": directive_text,
        "本章大纲（可为空）": outline_text,
        "待检查正文": body,
        "程序构造的逐项核对计划（ID不得增删）": plan_text,
        "截断范围": {"正文": body_truncated, "Story Bible": bible_truncated,
                     "前章资料": prior.truncated, "章前指令": directive_truncated,
                     "大纲": outline_truncated, "核对计划": plan_truncated},
    }
    try:
        raw = (llm if provider is None else provider).complete(
            [
                {"role": "system", "content": STORY_CHECK_SYSTEM},
                {"role": "user", "content": STORY_CHECK_USER.format(
                    materials=json.dumps(materials, ensure_ascii=False),
                )},
            ],
            temperature=0.1, max_tokens=min(8000, max(3500, 1800 + len(plan) * 180)),
        )
    except Exception:
        # Provider 异常可能含私有服务 URL 或鉴权信息，不把原文交给界面。
        raise HTTPException(status_code=502, detail="剧情复核服务暂不可用，请稍后重试") from None

    try:
        parsed = _parse_json(raw)
        if not isinstance(parsed, dict) or not ({"checks", "requirement_checks"} & parsed.keys()):
            raise ValueError("缺少核对列表")
        response = _StoryReviewResponse.model_validate(parsed)
        if any(not check.requirement.strip() or not check.reason.strip() for check in response.checks):
            raise ValueError("问题缺少实质依据")
        if any(not check.requirement_id.strip() or not check.reason.strip() for check in response.requirement_checks):
            raise ValueError("核对项缺少实质依据")
        if not plan and not response.checks:
            raise ValueError("没有任何检查结论")
    except (ValueError, TypeError, ValidationError, AttributeError):
        raise HTTPException(status_code=502, detail="模型返回的剧情复核格式无效，请重试") from None

    complete = (not truncated
                and not any(check.status == "uncertain" for check in response.checks))
    trustworthy: list[StoryCheckIssue] = []
    by_id = {item.id: item for item in plan}
    counts = Counter(check.requirement_id for check in response.requirement_checks)
    returned: dict[str, StoryRequirementCheck] = {}
    unreviewed: set[str] = {item.id for item in plan if counts[item.id] != 1}
    for check in response.requirement_checks:
        item = by_id.get(check.requirement_id)
        if item is None:
            complete = False
            continue
        if counts[item.id] != 1 or (check.category is not None and check.category != item.source):
            unreviewed.add(item.id)
            continue
        if check.excerpt and (check.excerpt not in body or check.excerpt not in content):
            unreviewed.add(item.id)
            continue
        if check.status == "satisfied" and item.evidence_required and not check.excerpt.strip():
            unreviewed.add(item.id)
            continue
        if check.status == "violation" and body_truncated and not check.excerpt:
            unreviewed.add(item.id)
            continue
        returned[item.id] = StoryRequirementCheck.model_validate(check.model_dump())
        if check.status == "uncertain":
            unreviewed.add(item.id)
        elif check.status == "violation":
            trustworthy.append(StoryCheckIssue(
                category=item.source, severity=check.severity, requirement=item.text[:800],
                excerpt=check.excerpt, reason=check.reason,
            ))
    for check in response.checks:
        if check.category not in {"setting", "continuity"}:
            complete = False
            continue
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
    complete = complete and not unreviewed and len(trustworthy) <= MAX_ISSUES
    return StoryCheckResult(
        issues=trustworthy[:MAX_ISSUES], truncated=truncated, review_complete=complete,
        history_truncated=prior.truncated, previous_chapter_title=prior.previous_title,
        chapter_title=chapter_title,
        requirements=[StoryRequirement(id=item.id, source=item.source, text=item.text) for item in plan],
        requirement_checks=[returned[item.id] for item in plan if item.id in returned],
        unreviewed_requirement_ids=[item.id for item in plan if item.id in unreviewed],
    )
