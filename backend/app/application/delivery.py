"""成品交付：统一范围解析、目录统计与零模型调用的导出前检查。"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.application.consistency import consistency_check
from app.application.revisions import count_words
from app.infrastructure import repository as repos


def _volume_id(node_id: str, nodes: dict) -> str:
    """沿大纲父链查找所属卷；无效或断开的绑定视为未归卷。"""
    seen: set[str] = set()
    while node_id and node_id not in seen:
        seen.add(node_id)
        node = nodes.get(node_id)
        if node is None:
            return ""
        if node.level == "volume":
            return node.id
        node_id = node.parent_id
    return ""


def book_summary(db: Session, novel_id: str) -> dict:
    novel = repos.NovelRepo(db).get(novel_id)
    chapters = repos.ChapterRepo(db).list_book_metadata(novel_id)
    nodes = {node.id: node for node in repos.OutlineRepo(db).list_all(novel_id)}
    entries = []
    for chapter in chapters:
        volume_id = _volume_id(chapter.outline_node_id, nodes)
        volume = nodes.get(volume_id)
        entries.append({
            "id": chapter.id,
            "seq": chapter.seq,
            "title": chapter.title,
            "status": chapter.status,
            "word_count": chapter.word_count,
            "outline_node_id": chapter.outline_node_id,
            "volume_id": volume_id,
            "volume_title": volume.title if volume else "",
            "tension_score": chapter.tension_score,
            "drift_score": chapter.drift_score,
        })
    return {
        "novel_id": novel.id,
        "title": novel.title,
        "chapter_count": len(entries),
        "word_count": sum(item["word_count"] for item in entries),
        "draft_count": sum(item["status"] == "draft" for item in entries),
        "finalized_count": sum(item["status"] == "finalized" for item in entries),
        "volumes": [
            {"id": node.id, "title": node.title}
            for node in nodes.values() if node.level == "volume"
        ],
        "chapters": entries,
    }


def resolve_scope(db: Session, novel_id: str, request=None) -> tuple[list, list, list]:
    """返回 (范围内全部章节, 实际导出章节, 全书未归卷章节)。"""
    scope = request.scope if request else "all"
    volume_id = request.volume_id if request else ""
    chapter_ids = request.chapter_ids if request else []
    include_drafts = request.include_drafts if request else False
    repos.NovelRepo(db).get(novel_id)
    chapters = repos.ChapterRepo(db).list(novel_id)
    nodes = {node.id: node for node in repos.OutlineRepo(db).list_all(novel_id)}
    unbound = [c for c in chapters if not _volume_id(c.outline_node_id, nodes)]

    if scope == "volume":
        node = nodes.get(volume_id)
        if not node or node.level != "volume":
            raise HTTPException(status_code=400, detail="请选择本作品中的卷")
        scoped = [c for c in chapters if _volume_id(c.outline_node_id, nodes) == node.id]
    elif scope == "chapters":
        ids = set(chapter_ids)
        if not ids or not ids.issubset({c.id for c in chapters}):
            raise HTTPException(status_code=400, detail="请选择至少一个本作品中的章节")
        scoped = [c for c in chapters if c.id in ids]
    else:
        scoped = chapters

    exportable = [c for c in scoped if include_drafts or c.status == "finalized"]
    return scoped, exportable, unbound


def preflight(db: Session, novel_id: str, request) -> dict:
    scoped, exportable, unbound = resolve_scope(db, novel_id, request)
    drafts = [
        {"id": c.id, "seq": c.seq, "title": c.title}
        for c in scoped if c.status == "draft"
    ]
    unresolved = [
        {"id": f.id, "content": f.content, "status": f.status}
        for f in repos.ForeshadowingRepo(db).list(novel_id) if f.status != "consumed"
    ]
    conflicts = [
        {"chapter_id": c.id, "seq": c.seq, "title": c.title, **item}
        for c in scoped
        for item in consistency_check(db, novel_id, c.content)["conflicts"]
    ]
    return {
        "chapter_count": len(exportable),
        "word_count": sum(count_words(c.content) for c in exportable),
        "draft_chapters": drafts,
        "unbound_chapters": (
            [{"id": c.id, "seq": c.seq, "title": c.title} for c in unbound]
            if request.scope == "all" else []
        ),
        "unresolved_foreshadowings": unresolved,
        "rule_conflicts": conflicts,
        "ready": not drafts and not conflicts,
    }
