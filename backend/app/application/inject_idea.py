"""对话式构思注入用例：意图识别 + 设定变更应用 + 大纲重规划。"""
import json

from sqlalchemy.orm import Session

from app.application.consistency import consistency_check
from app.domain.entities import OutlineLevel
from app.infrastructure import repository as repos
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


def _find_by_name(db: Session, novel_id: str, name: str):
    for kind, name_field in _NAME_FIELD.items():
        if not name_field:
            continue
        repo = _REPO_MAP[kind](db)
        for entity in repo.list(novel_id):
            if getattr(entity, name_field) == name:
                return {"type": kind, "entity": entity}
    return None


def _match_targets(db: Session, novel_id: str, targets) -> list[dict]:
    result = []
    for t in targets or []:
        name = t if isinstance(t, str) else (t.get("name") or t.get("title") or "")
        hit = _find_by_name(db, novel_id, name)
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
    return {
        "text": text,
        "targets": targets,
        "changes": changes,
        "reason": reason,
        "matched": _match_targets(db, novel_id, targets),
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


def _create_entity(db: Session, novel_id: str, kind: str, data: dict):
    obj = _CREATE_SCHEMA[kind](**data)
    return _REPO_MAP[kind](db).create(novel_id, **obj.model_dump())


def _update_entity(db: Session, novel_id: str, kind: str, entity_id: str, data: dict):
    obj = _UPDATE_SCHEMA[kind](**data)
    return _REPO_MAP[kind](db).update(
        novel_id, entity_id, **obj.model_dump(exclude_unset=True, mode="json")
    )


def _unfinished_chapter_nodes(db: Session, novel_id: str):
    nodes = repos.OutlineRepo(db).list_all(novel_id)
    chapters = repos.ChapterRepo(db).list(novel_id)
    generated_ids = {c.outline_node_id for c in chapters if c.outline_node_id}
    return [n for n in nodes if n.level == OutlineLevel.CHAPTER.value and n.id not in generated_ids]


def _replan_outline(db: Session, novel_id: str, changes: list[dict], updated_types: set) -> list[dict]:
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
    by_title = {n.title: n for n in unfinished}
    for u in updates:
        title = u.get("title")
        summary = u.get("summary")
        node = by_title.get(title) if isinstance(u, dict) else None
        if node and summary:
            repos.OutlineRepo(db).update(novel_id, node.id, summary=summary)
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


def apply_injections(db: Session, novel_id: str, suggestions) -> dict:
    """应用设定变更（支持 update 按名字匹配），并触发大纲重规划与一致性检查。"""
    changes = (
        suggestions
        if isinstance(suggestions, list)
        else (suggestions or {}).get("changes", [])
    )
    applied: list[dict] = []
    updated_types: set[str] = set()

    for ch in changes:
        if not isinstance(ch, dict):
            continue
        kind = ch.get("type")
        action = ch.get("action", "create")
        data = _normalize_data(kind, ch.get("data") or {})
        if kind not in _REPO_MAP:
            continue
        try:
            if action == "update":
                name_field = _NAME_FIELD.get(kind)
                name = data.get(name_field) if name_field else None
                hit = _find_by_name(db, novel_id, name) if name else None
                if hit and hit["type"] == kind:
                    _update_entity(db, novel_id, kind, hit["entity"].id, data)
                    applied.append({"type": kind, "action": "update", "id": hit["entity"].id})
                    updated_types.add(kind)
                else:
                    entity = _create_entity(db, novel_id, kind, data)
                    applied.append({"type": kind, "action": "create", "id": entity.id})
            else:
                entity = _create_entity(db, novel_id, kind, data)
                applied.append({"type": kind, "action": "create", "id": entity.id})
        except Exception:  # noqa: BLE001  跳过校验不通过的变更
            continue

    outline_updates = _replan_outline(db, novel_id, changes, updated_types)
    consistency = consistency_check(db, novel_id, _check_text(changes))

    return {"applied": applied, "outline_updates": outline_updates, "consistency": consistency}
