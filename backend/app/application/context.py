"""上下文装配用例：build_context。"""
from sqlalchemy.orm import Session

from app.infrastructure import repository as repos


def _join(*parts: str) -> str:
    return "；".join(p for p in parts if p)


def _id_to_name(db: Session, novel_id: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for char in repos.CharacterRepo(db).list(novel_id):
        mapping[char.id] = char.name
    for loc in repos.LocationRepo(db).list(novel_id):
        mapping[loc.id] = loc.name
    for ws in repos.WorldSettingRepo(db).list(novel_id):
        mapping[ws.id] = ws.name
    for ev in repos.TimelineEventRepo(db).list(novel_id):
        mapping[ev.id] = ev.title
    for f in repos.ForeshadowingRepo(db).list(novel_id):
        mapping[f.id] = f.content[:20]
    return mapping


def _settings_block(db: Session, novel_id: str) -> str:
    id_to_name = _id_to_name(db, novel_id)
    lines: list[str] = []

    for c in repos.CharacterRepo(db).list(novel_id):
        lines.append(
            f"人物[{c.name}]：{_join(c.role, c.personality, c.goal, c.appearance, c.catchphrase, c.known_info)}"
        )
    for loc in repos.LocationRepo(db).list(novel_id):
        lines.append(f"地点[{loc.name}]：{_join(loc.level, loc.description)}")
    for ws in repos.WorldSettingRepo(db).list(novel_id):
        lines.append(f"世界观[{ws.name}]：{_join(ws.category, ws.description)}")
    for ev in repos.TimelineEventRepo(db).list(novel_id):
        lines.append(f"事件[{ev.title}]（{ev.time_point}）：{ev.description}")
    for f in repos.ForeshadowingRepo(db).list(novel_id):
        lines.append(f"伏笔[{f.status}]：{f.content}")
    for rel in repos.RelationshipRepo(db).list(novel_id):
        subj = id_to_name.get(rel.subject_id, rel.subject_id)
        obj = id_to_name.get(rel.object_id, rel.object_id)
        lines.append(f"关系[{rel.relation_type}]：{subj} ↔ {obj}（{rel.note}）")

    return "\n".join(lines)


def _summary_chain(db: Session, novel_id: str) -> str:
    chapters = repos.ChapterRepo(db).finalized(novel_id)
    return "\n".join(f"第{c.seq}章：{c.summary}" for c in chapters if c.summary)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n…（已截断）"


def build_context(
    db: Session,
    novel_id: str,
    directive: str = "",
    chapter_position: str = "",
    max_chars: int = 4000,
) -> str:
    """按优先级装配单章生成上下文：章前指令 > 当前章定位 > 直接设定 > 摘要链。"""
    parts: list[str] = []
    if directive:
        parts.append("【章前指令（最高优先级，必须遵守）】\n" + directive)
    if chapter_position:
        parts.append("【当前章定位】\n" + chapter_position)

    settings = _settings_block(db, novel_id)
    if settings:
        parts.append("【Story Bible 设定】\n" + settings)

    chain = _summary_chain(db, novel_id)
    if chain:
        parts.append("【前情摘要链】\n" + chain)

    return _truncate("\n\n".join(parts), max_chars)
