"""一致性检查（规则库版）。

仅做确定性规则检测：伏笔状态、人物已知信息、时间线因果先后。
LLM 语义判断后置，阶段二不实现。
"""
from sqlalchemy.orm import Session

from app.infrastructure import repository as repos


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
