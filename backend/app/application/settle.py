"""定稿沉淀用例：settle_chapter + apply_suggestions。"""
import json

from sqlalchemy.orm import Session

from app.application.auto_pilot import check_drift
from app.application.consistency import consistency_check
from app.application.memory import write_chapter_memory
from app.application import setting_index
from app.domain.entities import ChapterStatus
from app.infrastructure.ai.llm_provider import LLMProvider
from app.infrastructure.ai.prompts import SETTLE_SYSTEM, SETTLE_USER
from app.infrastructure import repository as repos
from app.interfaces import schemas

llm = LLMProvider()


def _parse_json(raw: str) -> dict:
    """从 LLM 输出中稳健地提取 JSON 对象。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _build_suggestions(data: dict) -> list[dict]:
    suggestions: list[dict] = []
    for c in data.get("characters", []):
        if isinstance(c, dict) and c.get("name"):
            suggestions.append({"type": "character", "data": c})
    for e in data.get("events", []):
        if isinstance(e, dict) and e.get("title"):
            suggestions.append({"type": "timeline_event", "data": e})
    for f in data.get("foreshadowings", []):
        if isinstance(f, dict) and f.get("content"):
            suggestions.append({"type": "foreshadowing", "data": f})
    return suggestions


def settle_chapter(db: Session, novel_id: str, chapter_id: str) -> dict:
    """定稿一章：LLM 抽取摘要 + 新增人物/事件/伏笔建议，并落库摘要与状态。"""
    chapter = repos.ChapterRepo(db).get(novel_id, chapter_id)
    messages = [
        {"role": "system", "content": SETTLE_SYSTEM},
        {"role": "user", "content": SETTLE_USER.format(content=chapter.content)},
    ]
    raw = llm.complete(messages, temperature=0.2)
    data = _parse_json(raw)

    summary = (data.get("summary") or "").strip() or chapter.content[:100]
    repos.ChapterRepo(db).update(
        novel_id, chapter_id, status=ChapterStatus.FINALIZED.value, summary=summary
    )

    # 定稿后写入记忆（正文切片嵌入，失败降级不阻塞主流程）
    write_chapter_memory(db, novel_id, chapter_id, chapter.content)

    # 文风漂移：有基准才比对并写 drift_score，无基准则留空
    novel = repos.NovelRepo(db).get(novel_id)
    drift = None
    if (novel.style_anchor or "").strip():
        drift = check_drift(db, novel_id, chapter.content)
        if drift is not None:
            repos.ChapterRepo(db).update(novel_id, chapter_id, drift_score=drift)

    # 定稿主流程只跑规则层一致性检查，语义层由「深度检查」按需触发
    conflicts = consistency_check(db, novel_id, chapter.content).get("conflicts", [])

    return {
        "chapter_id": chapter_id,
        "summary": summary,
        "suggestions": _build_suggestions(data),
        "drift": drift,
        "conflicts": conflicts,
    }


def apply_suggestions(
    db: Session, novel_id: str, suggestions: list[dict], chapter_id: str = ""
) -> dict:
    """作者确认后，将设定变更建议落库到对应实体表。

    chapter_id 为当前定稿章节，落库伏笔时写入 planted_chapter_id。
    """
    created: list[dict] = []
    for s in suggestions:
        if not isinstance(s, dict):
            continue
        kind = s.get("type")
        data = s.get("data") or {}
        try:
            if kind == "character":
                obj = schemas.CharacterCreate(**data)
                entity = setting_index.create_setting(db, repos.CharacterRepo, novel_id, **obj.model_dump())
                created.append({"type": kind, "id": entity.id})
            elif kind == "timeline_event":
                obj = schemas.TimelineEventCreate(**data)
                entity = setting_index.create_setting(db, repos.TimelineEventRepo, novel_id, **obj.model_dump())
                created.append({"type": kind, "id": entity.id})
            elif kind == "foreshadowing":
                obj = schemas.ForeshadowingCreate(**data)
                payload = obj.model_dump()
                if chapter_id and not payload.get("planted_chapter_id"):
                    payload["planted_chapter_id"] = chapter_id
                entity = setting_index.create_setting(db, repos.ForeshadowingRepo, novel_id, **payload)
                created.append({"type": kind, "id": entity.id})
        except Exception:  # noqa: BLE001  跳过校验不通过的建议
            continue
    return {"created": created}
