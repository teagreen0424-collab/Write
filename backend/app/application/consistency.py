"""一致性检查。

规则层（同步、零成本）：伏笔状态、人物已知信息、时间线因果先后。
语义层（按需、每个有效分块一次 LLM 调用，最多 8 次）：deep_check 对照设定复核人设与时间线冲突，
仅由「深度检查」按钮触发，不进定稿主流程。
"""
import json

from sqlalchemy.orm import Session

from app.infrastructure import repository as repos
from app.infrastructure.ai.llm_provider import LLMProvider
from app.infrastructure.ai.prompts import DEEP_CHECK_SYSTEM, DEEP_CHECK_USER

llm = LLMProvider()

# 语义复核：约 800 字一块，单次上限 8 块
CHUNK_SIZE = 800
MAX_CHUNKS = 8


def _extract_secret(known_info: str) -> str:
    """从 known_info 中提取「不知道 X」里的 X。"""
    if "不知道" not in known_info:
        return ""
    tail = known_info.split("不知道", 1)[1]
    secret = tail.split("\n")[0].split("，")[0].split("。")[0].split("；")[0]
    return secret.strip("，。、；： ")


def rule_conflicts(content: str, foreshadowings, characters, events) -> list[dict]:
    """仅用已读取的事实运行三类字面规则；偏移按 Python Unicode 码点计数。"""
    conflicts: list[dict] = []

    # 1) 伏笔状态：已消费/已悬置的伏笔被再次使用
    for f in foreshadowings:
        if f.status in ("consumed", "suspended") and f.content and f.content in content:
            start = content.find(f.content)
            conflicts.append(
                {
                    "rule": "foreshadowing",
                    "message": f"伏笔「{f.content[:20]}」状态为 {f.status}，却被再次使用",
                    "start": start,
                    "end": start + len(f.content),
                    "entity_id": f.id,
                }
            )

    # 2) 人物已知信息：角色知道了「不知道 X」的信息
    for c in characters:
        secret = _extract_secret(c.known_info or "")
        if secret and len(secret) >= 2 and c.name and c.name in content and secret in content:
            start = content.find(secret)
            conflicts.append(
                {
                    "rule": "known_info",
                    "message": f"角色「{c.name}」知道了不该知道的信息",
                    "start": start,
                    "end": start + len(secret),
                    "entity_id": c.id,
                }
            )

    # 3) 时间线先后：因果事件都被提及但顺序倒置
    event_map = {e.id: e for e in events}
    for e in events:
        if not e.cause_id or not e.title:
            continue
        cause = event_map.get(e.cause_id)
        if not cause or not cause.title:
            continue
        if e.title in content and cause.title in content:
            pos_effect = content.find(e.title)
            pos_cause = content.find(cause.title)
            if pos_effect < pos_cause:
                conflicts.append(
                    {
                        "rule": "timeline",
                        "message": f"时间线先后矛盾：事件「{cause.title}」应发生在「{e.title}」之前",
                        "start": pos_effect,
                        "end": pos_effect + len(e.title),
                        "entity_id": e.id,
                    }
                )

    return conflicts


def consistency_check(db: Session, novel_id: str, content: str) -> dict:
    # 手动检查、自动驾驶和生成提示共用同一纯规则；保持原仓储读取行为。
    return {"conflicts": rule_conflicts(content,
        repos.ForeshadowingRepo(db).list(novel_id),
        repos.CharacterRepo(db).list(novel_id),
        repos.TimelineEventRepo(db).list(novel_id))}


# ================= 语义层（深度检查） =================


def _parse_json(raw: str) -> dict:
    """从 LLM 输出中稳健地提取 JSON 对象。"""
    text = (raw or "").strip()
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


def _split_chunks(content: str, size: int = CHUNK_SIZE) -> list[dict]:
    """按空行与段落聚合成约 size 字的块，返回 [{start, text}]。

    text 始终是 content 的连续子串，start 为其起始偏移，供规则层命中比对。
    """
    chunks: list[dict] = []
    buf = ""
    buf_start = None
    offset = 0
    for raw in content.split("\n"):
        if raw.strip() == "":
            # 空行：段落结束，先落一块
            if buf:
                chunks.append({"start": buf_start, "text": buf})
                buf = ""
                buf_start = None
        else:
            if buf_start is None:
                buf_start = offset
            buf = f"{buf}\n{raw}" if buf else raw
            if len(buf) >= size:
                chunks.append({"start": buf_start, "text": buf})
                buf = ""
                buf_start = None
        offset += len(raw) + 1  # +1 为被 split 掉的换行符

    if buf:
        chunks.append({"start": buf_start, "text": buf})

    # 超长块再按 size 硬切，保证每块约 800 字
    result: list[dict] = []
    for c in chunks:
        text = c["text"]
        start = c["start"]
        while len(text) > size * 2:
            result.append({"start": start, "text": text[:size]})
            start += size
            text = text[size:]
        result.append({"start": start, "text": text})
    return result


def _entity_names(db: Session, novel_id: str) -> list[str]:
    """参与比对的人物、地点、世界观条目名。"""
    names: list[str] = []
    for c in repos.CharacterRepo(db).list(novel_id):
        if c.name:
            names.append(c.name)
    for loc in repos.LocationRepo(db).list(novel_id):
        if loc.name:
            names.append(loc.name)
    for ws in repos.WorldSettingRepo(db).list(novel_id):
        if ws.name:
            names.append(ws.name)
    return names


def _setting_context(db: Session, novel_id: str, content: str) -> str:
    """按本作品实体与明确引用装配相关事实，不从展示文本猜名称或端点。"""
    groups = {
        "characters": repos.CharacterRepo(db).list(novel_id),
        "locations": repos.LocationRepo(db).list(novel_id),
        "world_settings": repos.WorldSettingRepo(db).list(novel_id),
        "events": repos.TimelineEventRepo(db).list(novel_id),
        "foreshadowings": repos.ForeshadowingRepo(db).list(novel_id),
    }
    by_kind = {kind: {row.id: row for row in rows} for kind, rows in groups.items()}
    by_id: dict[str, list] = {}
    selected: set[str] = set()
    for rows in groups.values():
        for row in rows:
            by_id.setdefault(row.id, []).append(row)
            label = getattr(row, "name", None) or getattr(row, "title", None) or getattr(row, "content", "")
            if label and label in content:
                selected.add(row.id)
    mentioned_characters = selected.intersection(by_kind["characters"])

    def local_ids(value, kind):
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str) and item in by_kind[kind]]

    # 人物相关事件与伏笔也是判断依据，即使正文没有照抄事件标题或伏笔整句。
    for event in groups["events"]:
        if mentioned_characters.intersection(local_ids(event.participants, "characters")):
            selected.add(event.id)
    for foreshadowing in groups["foreshadowings"]:
        if mentioned_characters.intersection(local_ids(foreshadowing.related_characters, "characters")):
            selected.add(foreshadowing.id)

    relationships = []
    for relationship in repos.RelationshipRepo(db).list(novel_id):
        endpoints = (relationship.subject_id, relationship.object_id)
        # 缺失、其它作品或类型歧义的旧端点不能靠名称猜绑定。
        if not all(len(by_id.get(endpoint, [])) == 1 for endpoint in endpoints):
            continue
        if selected.intersection(endpoints):
            relationships.append(relationship)
    for relationship in relationships:
        selected.update((relationship.subject_id, relationship.object_id))

    # 仅补选中事实的明确依赖，不沿新对端无限扩展所有关系。
    expanded = set()
    while selected - expanded:
        for item_id in list(selected - expanded):
            expanded.add(item_id)
            for row in by_id.get(item_id, []):
                if item_id in by_kind["events"] and row is by_kind["events"][item_id]:
                    selected.update(local_ids(row.participants, "characters"))
                    if row.cause_id in by_kind["events"]:
                        selected.add(row.cause_id)
                elif item_id in by_kind["foreshadowings"] and row is by_kind["foreshadowings"][item_id]:
                    selected.update(local_ids(row.related_characters, "characters"))
                elif item_id in by_kind["locations"] and row is by_kind["locations"][item_id]:
                    if row.parent_id in by_kind["locations"]:
                        selected.add(row.parent_id)

    chapter_ids = set()
    if selected.intersection(by_kind["foreshadowings"]):
        chapter = repos.ChapterRepo.model
        chapter_ids = {row.id for row in db.query(chapter.id).filter(chapter.novel_id == novel_id).all()}

    def fact(row):
        fields = {column.key: getattr(row, column.key) for column in row.__table__.columns
                  if column.key not in {"novel_id", "created_at", "updated_at"}}
        if row.id in by_kind["events"] and row is by_kind["events"][row.id]:
            fields["participants"] = local_ids(row.participants, "characters")
            fields["cause_id"] = row.cause_id if row.cause_id in by_kind["events"] else ""
        elif row.id in by_kind["foreshadowings"] and row is by_kind["foreshadowings"][row.id]:
            fields["related_characters"] = local_ids(row.related_characters, "characters")
            for field in ("planted_chapter_id", "consumed_chapter_id"):
                fields[field] = getattr(row, field) if getattr(row, field) in chapter_ids else ""
        elif row.id in by_kind["locations"] and row is by_kind["locations"][row.id]:
            fields["parent_id"] = row.parent_id if row.parent_id in by_kind["locations"] else ""
        return fields

    context = {kind: [fact(row) for row in rows if row.id in selected] for kind, rows in groups.items()}
    context["relationships"] = [fact(row) for row in relationships]
    return json.dumps(context, ensure_ascii=False, separators=(",", ":"))


def _validated_conflicts(data, chunk: str, index: int) -> list[dict]:
    """整块结构及引文全部有效后才采用，坏条目不能伪装成无冲突的成功检查。"""
    if not isinstance(data, dict) or not isinstance(data.get("conflicts"), list):
        raise ValueError("模型结果必须是包含conflicts数组的JSON对象")
    result = []
    for item in data["conflicts"]:
        if not isinstance(item, dict):
            raise ValueError("冲突项必须是JSON对象")
        if any(not isinstance(item.get(field), str) or not item[field].strip()
               for field in ("type", "excerpt", "reason")):
            raise ValueError("冲突类型、引文及依据必须是非空字符串")
        if item.get("severity") not in ("high", "medium", "low"):
            raise ValueError("冲突严重度无效")
        if item["excerpt"] not in chunk:
            raise ValueError("冲突引文不在当前正文块中")
        result.append({"rule": "semantic", "type": item["type"], "excerpt": item["excerpt"],
                       "reason": item["reason"], "severity": item["severity"], "chunk_index": index})
    return result


def deep_check(db: Session, novel_id: str, content: str) -> dict:
    """LLM 语义复核：切块 → 跳过规则层已命中块与无实体名块 → 送 LLM。

    返回语义/规则冲突、实际成功/失败/跳过/未覆盖块数与状态；结果不落库。
    """
    chunks = _split_chunks(content)
    # 1) 规则层已命中的块不再重复送 LLM
    with db.no_autoflush:
        repos.NovelRepo(db).get(novel_id)
        rule_conflicts = consistency_check(db, novel_id, content)["conflicts"]
        names = _entity_names(db, novel_id)
    hit_ranges = [(c["start"], c["end"]) for c in rule_conflicts]
    # 2) 块内无人物/地点/世界观条目名的跳过

    eligible: list[tuple[int, dict]] = []
    skipped_rule = 0
    skipped_no_entity = 0
    for index, chunk in enumerate(chunks):
        start = chunk["start"]
        end = start + len(chunk["text"])
        if any(start < h_end and end > h_start for h_start, h_end in hit_ranges):
            skipped_rule += 1
            continue
        if not any(name in chunk["text"] for name in names):
            skipped_no_entity += 1
            continue
        eligible.append((index, chunk))

    to_check = eligible[:MAX_CHUNKS]
    unchecked = len(eligible) - len(to_check)
    with db.no_autoflush:
        setting = _setting_context(db, novel_id, content) if to_check else ""
    conflicts: list[dict] = []
    checked = 0
    failed = 0
    warnings: list[str] = []
    for index, chunk in to_check:
        try:
            raw = llm.complete(
                [
                    {"role": "system", "content": DEEP_CHECK_SYSTEM},
                    {
                        "role": "user",
                        "content": DEEP_CHECK_USER.format(
                            setting=setting, chunk=chunk["text"]
                        ),
                    },
                ],
                temperature=0.2,
            )
            data = _parse_json(raw)
            validated = _validated_conflicts(data, chunk["text"], index)
        except Exception:  # noqa: BLE001  单块失败不影响其余块
            failed += 1
            warnings.append(f"第 {index + 1} 块语义检查未完成：模型调用或返回结果未通过核验，请重试或人工核对。")
            continue
        conflicts.extend(validated)
        checked += 1

    if unchecked:
        warnings.append(f"语义检查最多调用 {MAX_CHUNKS} 块，另有 {unchecked} 块尚未检查；已查结果不能代表未覆盖正文。")
    if skipped_rule:
        warnings.append(f"{skipped_rule} 块已有规则命中，未再调用语义模型；请核对返回的规则提示。")
    if skipped_no_entity:
        warnings.append(f"{skipped_no_entity} 块未出现本作品人物、地点或世界观名称，已跳过语义检查。")
    if not to_check:
        status = "skipped"
    elif not checked:
        status = "failed"
    elif failed or unchecked:
        status = "partial"
    else:
        status = "complete"
    return {
        "conflicts": conflicts,
        "rule_conflicts": rule_conflicts,
        "status": status,
        "chunks_total": len(chunks),
        "chunks_eligible": len(eligible),
        "chunks_attempted": len(to_check),
        "chunks_checked": checked,
        "chunks_failed": failed,
        "chunks_skipped": skipped_rule + skipped_no_entity,
        "chunks_skipped_rule": skipped_rule,
        "chunks_skipped_no_entity": skipped_no_entity,
        "chunks_unchecked": unchecked,
        "truncated": bool(unchecked),
        "warnings": warnings,
    }
