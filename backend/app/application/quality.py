"""质量治理用例：全书质量曲线与伏笔追踪聚合。"""
from sqlalchemy.orm import Session

from app.application.auto_pilot import DRIFT_THRESHOLD
from app.domain.entities import ForeshadowStatus
from app.infrastructure.repository import ChapterRepo, ForeshadowingRepo, NovelRepo

# 长期未闭合阈值：悬置章数 ≥ 10 章，或 ≥ 已写章节数的 30%，先到者为准
LONG_PENDING_CHAPTERS = 10
LONG_PENDING_RATIO = 0.3


def build_curve(db: Session, novel_id: str) -> dict:
    """全书质量曲线：每章张力分与漂移分，按 seq 升序。"""
    novel = NovelRepo(db).get(novel_id)
    chapters = ChapterRepo(db).list(novel_id)
    return {
        "chapters": [
            {
                "seq": c.seq,
                "title": c.title,
                "status": c.status,
                "tension_score": c.tension_score,
                "drift_score": c.drift_score,
            }
            for c in chapters
        ],
        "anchor_set": bool((novel.style_anchor or "").strip()),
        "drift_threshold": DRIFT_THRESHOLD,
    }


def _seq_key(item: dict) -> tuple:
    """按埋设章节升序，planted_seq 为空的排末尾。"""
    planted = item["planted_seq"]
    return (planted is None, planted if planted is not None else 0)


def _suspended_key(item: dict) -> tuple:
    """悬置组：长期未闭合优先，再按悬置章数降序，planted_seq 为空的排末尾。"""
    planted = item["planted_seq"]
    suspended = item["suspended_chapters"]
    return (
        planted is None,
        not item["long_pending"],
        -(suspended if suspended is not None else 0),
    )


def aggregate_foreshadowings(db: Session, novel_id: str) -> dict:
    """伏笔追踪聚合：按状态分组并派生埋设序号与悬置章数。"""
    NovelRepo(db).get(novel_id)
    chapters = ChapterRepo(db).list(novel_id)
    # 「已写章节」口径：正文非空的章节
    written = [c for c in chapters if (c.content or "").strip()]
    seq_by_chapter = {c.id: c.seq for c in written}
    latest_seq = max(seq_by_chapter.values(), default=0)
    chapter_count = len(written)

    groups: dict[str, list] = {"planted": [], "suspended": [], "consumed": []}
    for fs in ForeshadowingRepo(db).list(novel_id):
        planted_seq = seq_by_chapter.get(fs.planted_chapter_id)
        consumed_seq = seq_by_chapter.get(fs.consumed_chapter_id)
        if planted_seq is None:
            suspended_chapters = None
        elif fs.status == ForeshadowStatus.CONSUMED.value:
            suspended_chapters = (
                consumed_seq - planted_seq - 1 if consumed_seq is not None else None
            )
        else:
            suspended_chapters = latest_seq - planted_seq
        long_pending = bool(
            fs.status == ForeshadowStatus.SUSPENDED.value
            and suspended_chapters is not None
            and (
                suspended_chapters >= LONG_PENDING_CHAPTERS
                or suspended_chapters >= LONG_PENDING_RATIO * chapter_count
            )
        )
        item = {
            "id": fs.id,
            "content": fs.content,
            "status": fs.status,
            "related_characters": fs.related_characters,
            "planted_chapter_id": fs.planted_chapter_id,
            "consumed_chapter_id": fs.consumed_chapter_id,
            "planted_seq": planted_seq,
            "suspended_chapters": suspended_chapters,
            "long_pending": long_pending,
        }
        groups.get(fs.status, groups["planted"]).append(item)

    groups["suspended"].sort(key=_suspended_key)
    groups["planted"].sort(key=_seq_key)
    groups["consumed"].sort(key=_seq_key)
    return {
        "groups": groups,
        "latest_seq": latest_seq,
        "chapter_count": chapter_count,
        "long_pending_threshold": {
            "chapters": LONG_PENDING_CHAPTERS,
            "ratio": LONG_PENDING_RATIO,
        },
    }
