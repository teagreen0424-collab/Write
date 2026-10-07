"""对话式构思注入用例：意图识别 + 设定变更应用 + 大纲重规划。"""
import json

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.application.consistency import consistency_check
from app.application.memory import record_injection
from app.application import setting_index
from app.application.reference_resolution import resolve_references, failure
from app.domain.entities import ForeshadowStatus, OutlineLevel
from app.infrastructure import repository as repos
from app.infrastructure import models
from app.infrastructure.ai.llm_provider import LLMProvider
from app.infrastructure.ai.prompts import (
    INJECT_IDEA_SYSTEM,
    INJECT_IDEA_USER,
    INJECT_REPLAN_SYSTEM,
    INJECT_REPLAN_USER,
)
from app.interfaces import schemas

llm = LLMProvider()

_REPO_MAP = {
    "character": repos.CharacterRepo,
    "location": repos.LocationRepo,
    "world_setting": repos.WorldSettingRepo,
    "timeline_event": repos.TimelineEventRepo,
    "foreshadowing": repos.ForeshadowingRepo,
    "relationship": repos.RelationshipRepo,
}
_CREATE_SCHEMA = {
    "character": schemas.CharacterCreate,
    "location": schemas.LocationCreate,
    "world_setting": schemas.WorldSettingCreate,
    "timeline_event": schemas.TimelineEventCreate,
    "foreshadowing": schemas.ForeshadowingCreate,
    "relationship": schemas.RelationshipCreate,
}
_UPDATE_SCHEMA = {
    "character": schemas.CharacterUpdate,
    "location": schemas.LocationUpdate,
    "world_setting": schemas.WorldSettingUpdate,
    "timeline_event": schemas.TimelineEventUpdate,
    "foreshadowing": schemas.ForeshadowingUpdate,
    "relationship": schemas.RelationshipUpdate,
}
# 各类实体用于「按名字匹配」的字段
_NAME_FIELD = {
    "character": "name",
    "location": "name",
    "world_setting": "name",
    "timeline_event": "title",
    "foreshadowing": "content",
    "relationship": None,
}


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


def _find_by_name(db: Session, novel_id: str, name: str, target_kind: str | None = None):
    matches = []
    for kind, name_field in _NAME_FIELD.items():
        if not name_field or (target_kind and kind != target_kind):
            continue
        repo = _REPO_MAP[kind](db)
        for entity in repo.list(novel_id):
            if getattr(entity, name_field) == name:
                matches.append({"type": kind, "entity": entity})
    if len(matches) > 1:
        raise HTTPException(400, detail=f"目标“{name}”有多个同名记录，请先在设定面板区分名称")
    return matches[0] if matches else None


def _match_targets(db: Session, novel_id: str, targets) -> list[dict]:
    result = []
    for t in targets or []:
        name = t if isinstance(t, str) else (t.get("name") or t.get("title") or "")
        try:
            hit = _find_by_name(db, novel_id, name)
        except HTTPException:
            result.append({"name": name, "matched": False, "ambiguous": True})
            continue
        if hit:
            result.append({"name": name, "matched": True, "type": hit["type"], "id": hit["entity"].id})
        else:
            result.append({"name": name, "matched": False})
    return result


def inject_idea(db: Session, novel_id: str, text: str) -> dict:
    """意图识别：自然语言 → 结构化变更建议 + 目标匹配结果。"""
    messages = [
        {"role": "system", "content": INJECT_IDEA_SYSTEM},
        {"role": "user", "content": INJECT_IDEA_USER.format(text=text)},
    ]
    raw = llm.complete(messages, temperature=0.2)
    data = _parse_json(raw)
    targets = data.get("targets") or []
    changes = data.get("changes") or []
    reason = data.get("reason") or ""
    matched = _match_targets(db, novel_id, targets)
    # 模型偶尔把首次出现的人物标成 update，且只在 targets 中给出姓名。
    # 预览中补齐唯一目标并展示实际将执行的 create/update，仍由作者确认。
    for change in changes:
        if not isinstance(change, dict):
            continue
        item = change.get("data")
        if not isinstance(item, dict):
            continue
        if change.get("type") == "foreshadowing":
            if item.get("status") not in {status.value for status in ForeshadowStatus}:
                item["status"] = ForeshadowStatus.PLANTED.value
            continue
        if change.get("type") != "character":
            continue
        if not item.get("name") and len(matched) == 1:
            target = matched[0]
            if not target["matched"] or target.get("type") == "character":
                item["name"] = target["name"]
    failed = []
    for index, change in enumerate(changes):
        if not isinstance(change, dict):
            failed.append(failure(index, "", ValueError("变更必须是 JSON 对象")))
            continue
        kind = change.get("type", "")
        try:
            if not isinstance(kind, str) or kind not in _REPO_MAP:
                raise ValueError(f"不支持的设定类型：{kind}")
            change["data"] = resolve_references(db, novel_id, kind, change.get("data", {}))
            name_field = _NAME_FIELD.get(kind) if isinstance(kind, str) else None
            name = change["data"].get(name_field) if name_field else None
            if change.get("action") == "update" and name:
                hit = _find_by_name(db, novel_id, name, target_kind=kind)
                if not hit:
                    change["action"] = "create"
        except Exception as error:
            failed.append(failure(index, str(kind or ""), error))
    return {
        "text": text,
        "targets": targets,
        "changes": changes,
        "reason": reason,
        "matched": matched,
        "failed": failed,
    }


def _normalize_data(kind: str, data: dict) -> dict:
    """归一化不同实体的名字字段：LLM 可能统一用 name，需映射到 title/content。"""
    if not isinstance(data, dict):
        return data
    data = dict(data)
    if kind == "timeline_event" and not data.get("title") and data.get("name"):
        data["title"] = data["name"]
        data.pop("name", None)
    elif kind == "foreshadowing" and not data.get("content") and data.get("name"):
        data["content"] = data["name"]
        data.pop("name", None)
    return data


def _create_entity(db: Session, novel_id: str, kind: str, data: dict, *, commit: bool = True):
    obj = _CREATE_SCHEMA[kind](**data)
    return setting_index.create_setting(db, _REPO_MAP[kind], novel_id, commit=commit, **obj.model_dump())


def _update_entity(
    db: Session, novel_id: str, kind: str, entity_id: str, data: dict, *, commit: bool = True,
):
    obj = _UPDATE_SCHEMA[kind](**data)
    return setting_index.update_setting(
        db, _REPO_MAP[kind], novel_id, entity_id, commit=commit,
        **obj.model_dump(exclude_unset=True, mode="json"),
    )


def _unfinished_chapter_nodes(db: Session, novel_id: str):
    nodes = repos.OutlineRepo(db).list_all(novel_id)
    chapters = repos.ChapterRepo(db).list(novel_id)
    generated_ids = {c.outline_node_id for c in chapters if c.outline_node_id}
    return [n for n in nodes if n.level == OutlineLevel.CHAPTER.value and n.id not in generated_ids]


def _replan_outline(
    db: Session, novel_id: str, changes: list[dict], updated_types: set,
    *, commit: bool = True,
) -> list[dict]:
    """对影响人物/世界观等的 update 变更，调整未生成章节大纲的概要。"""
    if not (updated_types & {"character", "world_setting", "location"}):
        return []
    unfinished = _unfinished_chapter_nodes(db, novel_id)
    if not unfinished:
        return []

    outline_text = "\n".join(f"{n.title}：{n.summary or '无'}" for n in unfinished)
    changes_text = json.dumps(changes, ensure_ascii=False)
    messages = [
        {"role": "system", "content": INJECT_REPLAN_SYSTEM},
        {
            "role": "user",
            "content": INJECT_REPLAN_USER.format(changes=changes_text, outline=outline_text),
        },
    ]
    raw = llm.complete(messages, temperature=0.3)
    data = _parse_json(raw)
    updates = data.get("outline_updates") or []

    applied = []
    by_title = {}
    for node in unfinished:
        by_title.setdefault(node.title, []).append(node)
    for u in updates:
        if not isinstance(u, dict):
            raise ValueError("大纲调整结果必须是 JSON 对象")
        title = u.get("title")
        summary = u.get("summary")
        candidates = by_title.get(title, [])
        if summary and len(candidates) > 1:
            raise HTTPException(400, detail=f"未生成大纲有多个同名章节“{title}”，请先区分标题再调整")
        node = candidates[0] if candidates else None
        if node and summary:
            if commit:
                repos.OutlineRepo(db).update(novel_id, node.id, summary=summary)
            else:
                node.summary = summary
                db.flush()
            applied.append({"node_id": node.id, "title": title, "summary": summary})
    return applied


def _check_text(changes: list[dict]) -> str:
    parts: list[str] = []
    for ch in changes:
        if not isinstance(ch, dict):
            continue
        data = ch.get("data") or {}
        if isinstance(data, dict):
            parts.extend(str(v) for v in data.values())
    return " ".join(parts)


def apply_injections(
    db: Session,
    novel_id: str,
    suggestions,
    raw_text: str = "",
    channel: str = "chat",
    atomic: bool = False,
) -> dict:
    """应用设定变更（支持 update 按名字匹配），并触发大纲重规划与一致性检查。"""
    changes = (
        suggestions
        if isinstance(suggestions, list)
        else (suggestions or {}).get("changes", [])
    )
    applied: list[dict] = []
    failed: list[dict] = []
    applied_changes: list[dict] = []
    updated_types: set[str] = set()

    for index, ch in enumerate(changes):
        if not isinstance(ch, dict):
            if atomic:
                db.rollback()
                raise ValueError("变更必须是 JSON 对象")
            failed.append(failure(index, "", ValueError("变更必须是 JSON 对象")))
            continue
        kind = ch.get("type")
        action = ch.get("action", "create")
        data = _normalize_data(kind, ch.get("data") or {})
        if not isinstance(kind, str) or kind not in _REPO_MAP:
            if atomic:
                db.rollback()
                raise ValueError(f"不支持的设定类型：{kind}")
            failed.append(failure(index, str(kind or ""), ValueError(f"不支持的设定类型：{kind}")))
            continue
        try:
            if not isinstance(action, str) or action not in {"create", "update"}:
                raise ValueError(f"不支持的变更动作：{action}")
            data = resolve_references(db, novel_id, kind, data)
            if action == "update":
                name_field = _NAME_FIELD.get(kind)
                name = data.get(name_field) if name_field else None
                hit = _find_by_name(db, novel_id, name, target_kind=kind) if name else None
                if hit and hit["type"] == kind:
                    _update_entity(db, novel_id, kind, hit["entity"].id, data, commit=not atomic)
                    applied.append({"type": kind, "action": "update", "id": hit["entity"].id})
                    updated_types.add(kind)
                else:
                    entity = _create_entity(db, novel_id, kind, data, commit=not atomic)
                    applied.append({"type": kind, "action": "create", "id": entity.id})
            else:
                entity = _create_entity(db, novel_id, kind, data, commit=not atomic)
                applied.append({"type": kind, "action": "create", "id": entity.id})
            applied[-1]["index"] = index
            applied_changes.append({**ch, "data": data})
        except Exception as error:  # noqa: BLE001  旧接口返回逐条错误；原子路径整单失败
            if atomic:
                db.rollback()
                raise
            db.rollback()
            failed.append(failure(index, kind, error))
            continue

    warnings = []
    outline_updates = []
    if applied and updated_types & {"character", "world_setting", "location"}:
        try:
            outline_updates = _replan_outline(db, novel_id, applied_changes, updated_types, commit=False)
            if not atomic:
                db.commit()
        except Exception as error:
            db.rollback()
            if atomic:
                raise
            detail = failure(-1, "outline", error)["error"]
            warnings.append(f"设定变更已保存，但后续大纲调整失败：{detail}。请核对大纲")
    consistency = {"conflicts": []}
    if applied:
        try:
            consistency = consistency_check(db, novel_id, _check_text(applied_changes))
        except Exception:
            db.rollback()
            if atomic:
                raise
            consistency = {"conflicts": [], "status": "failed"}
            warnings.append("设定变更已保存，但一致性检查失败；请稍后重新检查，请勿重复应用已保存项")

    # 记录构思注入（raw_text 非空时落 injection 表）
    if raw_text and applied:
        if atomic:
            db.add(models.Injection(
                novel_id=novel_id, channel=channel, raw_text=raw_text,
                parsed_intent=changes, applied_changes=applied,
            ))
            db.flush()
        else:
            try:
                record_injection(
                    db, novel_id, channel, raw_text,
                    parsed_intent=changes, applied_changes=applied,
                )
            except Exception:
                db.rollback()
                warnings.append("设定变更已保存，但构思留痕失败；请勿重复应用已保存项")

    return {"applied": applied, "failed": failed, "warnings": warnings,
            "outline_updates": outline_updates, "consistency": consistency}
