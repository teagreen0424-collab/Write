"""一致性检查。

规则层（同步、零成本）：伏笔状态、人物已知信息、时间线因果先后。
语义层（按需、每个有效分块一次 LLM 调用，最多 8 次）：deep_check 对照设定复核人设与时间线冲突，
仅由「深度检查」按钮触发，不进定稿主流程。
"""
import json
import re

from sqlalchemy.orm import Session

from app.application.context import _settings_block
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


def consistency_check(db: Session, novel_id: str, content: str) -> dict:
    conflicts: list[dict] = []

    # 1) 伏笔状态：已消费/已悬置的伏笔被再次使用
    for f in repos.ForeshadowingRepo(db).list(novel_id):
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
    for c in repos.CharacterRepo(db).list(novel_id):
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
    events = repos.TimelineEventRepo(db).list(novel_id)
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

    return {"conflicts": conflicts}


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
    """复用 build_context 的设定装配结果，只保留在正文中出现过的实体。"""
    kept: list[str] = []
    for line in _settings_block(db, novel_id).split("\n"):
        left = line.find("[")
        right = line.find("]")
        name = line[left + 1 : right] if left != -1 and right > left else ""
        if name and name in content:
            kept.append(line)
    return "\n".join(kept)


def deep_check(db: Session, novel_id: str, content: str) -> dict:
    """LLM 语义复核：切块 → 跳过规则层已命中块与无实体名块 → 送 LLM。

    返回 {conflicts, chunks_checked, chunks_skipped, truncated}。
    结果不落库。
    """
    chunks = _split_chunks(content)
    # 1) 规则层已命中的块不再重复送 LLM
    rule_conflicts = consistency_check(db, novel_id, content)["conflicts"]
    hit_ranges = [(c["start"], c["end"]) for c in rule_conflicts]
    # 2) 块内无人物/地点/世界观条目名的跳过
    names = _entity_names(db, novel_id)

    eligible: list[tuple[int, dict]] = []
    skipped = 0
    for index, chunk in enumerate(chunks):
        start = chunk["start"]
        end = start + len(chunk["text"])
        if any(start < h_end and end > h_start for h_start, h_end in hit_ranges):
            skipped += 1
            continue
        if not any(name in chunk["text"] for name in names):
            skipped += 1
            continue
        eligible.append((index, chunk))

    to_check = eligible[:MAX_CHUNKS]
    truncated = bool(len(eligible) > MAX_CHUNKS)

    setting = _setting_context(db, novel_id, content)
    conflicts: list[dict] = []
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
        except Exception:  # noqa: BLE001  单块失败不影响其余块
            continue
        for item in data.get("conflicts", []) or []:
            if not isinstance(item, dict):
                continue
            conflicts.append(
                {
                    "rule": "semantic",
                    "type": item.get("type", ""),
                    "excerpt": item.get("excerpt", ""),
                    "reason": item.get("reason", ""),
                    "severity": item.get("severity", "medium"),
                    "chunk_index": index,
                }
            )

    return {
        "conflicts": conflicts,
        "chunks_checked": len(to_check),
        "chunks_skipped": skipped,
        "truncated": truncated,
    }
