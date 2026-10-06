"""上下文装配用例：build_context。"""
from dataclasses import dataclass

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


def ordered_chapter_nodes(db: Session, novel_id: str) -> list:
    """复用卷、幕、章树序；先后关系不能由数据库创建时间猜测。"""
    by_parent: dict[str, list] = {}
    for node in repos.OutlineRepo(db).list_all(novel_id):
        by_parent.setdefault(node.parent_id, []).append(node)
    for children in by_parent.values():
        children.sort(key=lambda node: (node.sort_order, node.created_at, node.id))
    chapters: list = []

    def walk(parent_id: str) -> None:
        for node in by_parent.get(parent_id, []):
            if node.level == "chapter":
                chapters.append(node)
            walk(node.id)

    walk("")
    return chapters


def prior_chapters(
    db: Session, novel_id: str, chapter_seq: int | None = None,
    outline_node_id: str = "",
) -> list:
    """只读目标章之前的稿件；补历史缺章时用后继锚点限制序号上界。"""
    chapters = repos.ChapterRepo(db).list(novel_id)
    boundary = chapter_seq if chapter_seq is not None else (max((c.seq for c in chapters), default=0) + 1)
    order = {node.id: index for index, node in enumerate(ordered_chapter_nodes(db, novel_id))} if outline_node_id else {}
    target = order.get(outline_node_id)
    if target is not None:
        following = [c.seq for c in chapters if c.outline_node_id in order
                     and order[c.outline_node_id] >= target]
        if following:
            boundary = min(boundary, min(following))
    return [c for c in chapters if c.seq < boundary
            and (not outline_node_id or c.outline_node_id != outline_node_id)
            and (target is None or c.outline_node_id not in order
                 or order[c.outline_node_id] < target)]


def chapter_excerpt(content: str, max_chars: int = 7000) -> str:
    """限额含省略标记，保留章首人物建立和章尾衔接。"""
    if len(content) <= max_chars:
        return content
    marker = "\n\n…（上一章中段已省略）…\n\n"
    budget = max(0, max_chars - len(marker))
    head = min(3000, budget // 2)
    tail = budget - head
    return content[:head] + marker + (content[-tail:] if tail else "")


def recent_history(chapters: list, previous) -> str:
    """只用最近两章脉络，摘要和正文片段均限制在600字符。"""
    earlier = [c for c in chapters if c.seq < previous.seq][-2:]
    if not earlier:
        return "无（当前从第一章继续）"
    lines: list[str] = []
    for chapter in earlier:
        brief = (chapter.summary or chapter.content or "").strip()
        if len(brief) > 600:
            brief = brief[:299] + "…" + brief[-300:]
        status = "已定稿" if chapter.status == "finalized" else "未定稿前情"
        lines.append(f"第{chapter.seq}章《{chapter.title}》（{status}）：{brief or '无正文或摘要'}")
    return "\n".join(lines)


@dataclass(frozen=True)
class PriorChapterContext:
    text: str = ""
    truncated: bool = False
    previous_title: str = ""


def prior_chapter_context(
    db: Session, novel_id: str, chapter_seq: int | None = None,
    outline_node_id: str = "",
) -> PriorChapterContext:
    """草稿正文只用于承接与复核；不生成摘要、不定稿、不写设定。"""
    chapters = prior_chapters(db, novel_id, chapter_seq, outline_node_id)
    if not chapters:
        return PriorChapterContext()
    previous = chapters[-1]
    earlier = chapters[-3:-1]
    truncated = len(previous.content or "") > 7000 or any(
        len((c.summary or c.content or "").strip()) > 600 for c in earlier
    )
    status = "已定稿" if previous.status == "finalized" else "未定稿草稿"
    text = (
        "【前章衔接材料】\n"
        "以下草稿与摘要仅为待核对前情，不等于Story Bible定稿设定。"
        "Story Bible明确事实优先；若草稿与设定冲突，应核对，不得据此改写设定。\n"
        f"【较早两章脉络】\n{recent_history(chapters, previous)}\n\n"
        f"【必须直接承接的上一章】\n第{previous.seq}章《{previous.title}》（{status}）\n"
        f"{chapter_excerpt(previous.content or '')}"
    )
    return PriorChapterContext(text=text, truncated=truncated, previous_title=previous.title)


def _summary_chain(db: Session, novel_id: str, chapter_seq: int | None = None,
                   outline_node_id: str = "") -> str:
    chapters = prior_chapters(db, novel_id, chapter_seq, outline_node_id)
    return "\n".join(f"第{c.seq}章：{c.summary}" for c in chapters if c.status == "finalized" and c.summary)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n…（已截断）"


def _semantic_recall(db: Session, novel_id: str, query: str, top_k: int = 3,
                     eligible_chapter_ids: set[str] | None = None) -> str:
    """语义召回相关历史片段（失败降级为空字符串）。"""
    if not query:
        return ""
    try:
        from app.infrastructure.vector import search

        results = search(db, novel_id, query, top_k=top_k)
    except Exception:  # noqa: BLE001  检索失败降级，不影响主流程
        return ""
    if eligible_chapter_ids is not None:
        results = [r for r in results if not r.get("source_chapter_id")
                   or r["source_chapter_id"] in eligible_chapter_ids]
    if not results:
        return ""
    return "【相关历史片段】\n" + "\n".join(f"- {r['text']}" for r in results)


def build_context(
    db: Session,
    novel_id: str,
    directive: str = "",
    chapter_position: str = "",
    max_chars: int = 4000,
    *,
    chapter_seq: int | None = None,
    outline_node_id: str = "",
) -> str:
    """按优先级装配单章生成上下文：章前指令 > 当前章定位 > 直接设定 > 摘要链 > 语义召回。"""
    parts: list[str] = []
    if directive:
        parts.append("【章前指令（最高优先级，必须遵守）】\n" + directive)
    if chapter_position:
        parts.append("【当前章定位】\n" + chapter_position)

    settings = _settings_block(db, novel_id)
    if settings:
        parts.append("【Story Bible 设定】\n" + settings)

    chain = _summary_chain(db, novel_id, chapter_seq, outline_node_id)
    if chain:
        parts.append("【前情摘要链】\n" + chain)

    # 语义召回优先级最低，置于末尾（先被截断）
    query = " ".join(x for x in [directive, chapter_position] if x)
    eligible = {c.id for c in prior_chapters(db, novel_id, chapter_seq, outline_node_id)}
    recall = _semantic_recall(db, novel_id, query, eligible_chapter_ids=eligible)
    if recall:
        parts.append(recall)

    return _truncate("\n\n".join(parts), max_chars)
