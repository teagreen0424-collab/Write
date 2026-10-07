"""定稿沉淀用例：settle_chapter + apply_suggestions。"""
import json

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.application.auto_pilot import check_drift
from app.application.consistency import consistency_check
from app.application.memory import MemoryWriteResult, _slice_content, write_chapter_memory
from app.application import setting_index
from app.application.reference_resolution import resolve_references, failure
from app.application.settlement_receipts import SettleSourceChanged, apply_with_receipts, chapter_source_fingerprint
from app.domain.entities import ChapterStatus
from app.infrastructure.ai.llm_provider import LLMProvider
from app.infrastructure.ai.prompts import SETTLE_SYSTEM, SETTLE_USER
from app.infrastructure import repository as repos
from app.infrastructure import models
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
    if not isinstance(data, dict):
        raise ValueError("抽取结果必须是 JSON 对象")
    if data.get("summary") is not None and not isinstance(data["summary"], str):
        raise ValueError("摘要必须是文本")
    for field, required in (("characters", "name"), ("events", "title"), ("foreshadowings", "content")):
        values = data.get(field, [])
        if not isinstance(values, list):
            raise ValueError(f"{field} 必须是 JSON 数组")
        for value in values:
            if not isinstance(value, dict) or not isinstance(value.get(required), str) or not value[required].strip():
                raise ValueError(f"{field} 建议缺少有效的 {required}")
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


def settle_chapter(
    db: Session, novel_id: str, chapter_id: str, *, expected_author_fields: dict | None = None,
) -> dict:
    """定稿一章：LLM 抽取摘要 + 新增人物/事件/伏笔建议，并落库摘要与状态。"""
    # 列投影绕过旧ORM身份缓存，当前UI原稿与真实已保存稿不一致时不能提炼/定稿另一稿。
    row = db.execute(select(models.Chapter.title, models.Chapter.content, models.Chapter.directive).where(
        models.Chapter.novel_id == novel_id, models.Chapter.id == chapter_id,
    )).first()
    if row is None:
        raise HTTPException(404, detail="章节不存在")
    source = dict(row._mapping)
    if expected_author_fields is not None and source != expected_author_fields:
        raise SettleSourceChanged()
    content = source["content"]
    style_anchor = repos.NovelRepo(db).get(novel_id).style_anchor or ""
    messages = [
        {"role": "system", "content": SETTLE_SYSTEM},
        {"role": "user", "content": SETTLE_USER.format(content=content)},
    ]
    try:
        raw = llm.complete(messages, temperature=0.2)
    except Exception:  # noqa: BLE001  抽取失败发生在主写入前
        raise HTTPException(status_code=502, detail="定稿抽取服务暂不可用，请稍后重试；章节未改动") from None
    try:
        data = _parse_json(raw)
        suggestions = _build_suggestions(data)
        summary = (data.get("summary") or "").strip() or content[:100]
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=502, detail="模型返回的定稿抽取格式无效，请重试；章节未改动") from None

    # 模型等待期间允许另一请求保存；用数据库条件更新核对来源，不能凭缓存给新稿定稿。
    db.rollback()
    try:
        changed = db.execute(
            update(models.Chapter).where(
                models.Chapter.novel_id == novel_id,
                models.Chapter.id == chapter_id,
                *(getattr(models.Chapter, field) == value for field, value in source.items()),
            ).values(status=ChapterStatus.FINALIZED.value, summary=summary, drift_score=None)
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            raise SettleSourceChanged()
        db.commit()
    except OperationalError:
        db.rollback()
        raise HTTPException(status_code=409, detail="章节正在保存；本次定稿未应用，请核对最新稿后重试") from None
    except Exception:
        db.rollback()
        raise

    # 记忆降级不会撤销已保存定稿，但必须让作者知道当前可用片段与失败原因。
    try:
        memory = write_chapter_memory(db, novel_id, chapter_id, content)
    except Exception:  # noqa: BLE001  提交后读取/服务异常也不能冒充主写入失败
        db.rollback()
        total = len(_slice_content(content))
        memory = MemoryWriteResult(0, total, total, "storage_failed", [
            "章节及摘要已保存，但记忆沉淀未完成；召回会排除不匹配当前正文的旧片段，请稍后重试定稿沉淀。"
        ])
    warnings = list(memory.warnings)
    checks = {"drift": "skipped", "consistency": "completed"}

    # 文风漂移：有基准才比对并写 drift_score，无基准则留空
    drift = None
    if style_anchor.strip():
        try:
            score = check_drift(db, novel_id, content)
            if score is None:
                raise ValueError("漂移检查未返回结果")
            db.rollback()
            changed = db.execute(
                update(models.Chapter).where(
                    models.Chapter.novel_id == novel_id,
                    models.Chapter.id == chapter_id,
                    *(getattr(models.Chapter, field) == value for field, value in source.items()),
                ).values(drift_score=score).execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                db.rollback()
                checks["drift"] = "failed"
                warnings.append("文风漂移检查期间章节已变化；旧稿评分未写入当前章节，请重新检查最新稿。")
            else:
                db.commit()
                drift = score
                checks["drift"] = "completed"
        except Exception:  # noqa: BLE001  已提交主结果不因后置复核失败冒充整次失败
            db.rollback()
            checks["drift"] = "failed"
            warnings.append("章节及摘要已保存，但文风漂移检查未完成；请稍后重试检查。")

    # 定稿主流程只跑规则层一致性检查，语义层由「深度检查」按需触发
    conflicts = []
    try:
        conflicts = consistency_check(db, novel_id, content).get("conflicts", [])
        if not isinstance(conflicts, list):
            raise ValueError("规则检查未返回有效结果")
    except Exception:  # noqa: BLE001
        db.rollback()
        conflicts = []
        checks["consistency"] = "failed"
        warnings.append("章节及摘要已保存，但规则一致性检查未完成；当前不能确认没有冲突，请稍后重试检查。")

    return {
        "chapter_id": chapter_id,
        "source_fingerprint": chapter_source_fingerprint(novel_id, chapter_id, source),
        "summary": summary,
        "suggestions": suggestions,
        "drift": drift,
        "conflicts": conflicts,
        "memory": memory.as_dict(),
        "warnings": warnings,
        "checks": checks,
    }


def apply_suggestions(
    db: Session, novel_id: str, suggestions: list[dict], chapter_id: str = "",
    *, source_fingerprint: str | None = None,
) -> dict:
    """作者确认后，将设定变更建议落库到对应实体表。

    HTTP确认必须传原稿指纹，走持久逐项receipt。无指纹的旧内部帮助调用保留历史合同。
    chapter_id 为当前定稿章节，落库伏笔时写入 planted_chapter_id。
    """
    if source_fingerprint is not None:
        return apply_with_receipts(db, novel_id, chapter_id, suggestions, source_fingerprint)
    created: list[dict] = []
    failed: list[dict] = []
    repos.NovelRepo(db).get(novel_id)
    if chapter_id:
        repos.ChapterRepo(db).get(novel_id, chapter_id)
    for index, s in enumerate(suggestions):
        if not isinstance(s, dict):
            failed.append(failure(index, "", ValueError("建议必须是 JSON 对象")))
            continue
        kind = s.get("type")
        data = s.get("data") or {}
        try:
            if not isinstance(kind, str) or kind not in {"character", "timeline_event", "foreshadowing"}:
                raise ValueError(f"不支持的设定类型：{kind}")
            data = resolve_references(db, novel_id, kind, data)
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
            else:
                raise ValueError(f"不支持的设定类型：{kind}")
            created[-1]["index"] = index
        except Exception as error:  # noqa: BLE001  返回失败项，保留作者修正与重试的机会
            db.rollback()
            failed.append(failure(index, str(kind or ""), error))
            continue
    return {"created": created, "failed": failed}
