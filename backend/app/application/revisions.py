"""章节版本用例：覆盖前原子快照、安全恢复与派生数据失效。

版本只覆盖三个作者字段：title / content / directive。
status、summary、tension_score、drift_score 等派生状态只随快照留档供审阅，不参与「是否变化」的判断。

事务约定（不许绕过）：
- 快照插入与章节更新必须由同一次 `db.commit()` 完成；
- 禁止「先调用一个会 commit 的仓储方法写正文，再补写版本」这种伪原子写法；
- 例外：快照本身没有需要回滚的后续动作时，只插入不加其它写入，此时单独提交是安全的。
"""
from sqlalchemy.orm import Session

from app.domain.entities import ChapterStatus, RevisionSource
from app.infrastructure import models
from app.infrastructure.repository import ChapterRepo, ChapterRevisionRepo

# 判断版本变化的作者字段：只有这三个字段的实际变化才值得留档。
AUTHOR_FIELDS = ("title", "content", "directive")

# 快照来源。前两项与 domain.entities.RevisionSource 同源，后三项只由后端写入用例决定。
SOURCE_MANUAL_SAVE = RevisionSource.MANUAL_SAVE.value
SOURCE_REWRITE_APPLY = RevisionSource.REWRITE_APPLY.value
SOURCE_CONTINUE = "continue"
SOURCE_REWRITE = "rewrite"
SOURCE_RESTORE = "restore"


def count_words(text: str | None) -> int:
    """字数口径：去除所有空白字符后的字符数，与前端 countCharacters 一致。"""
    if not text:
        return 0
    return len("".join(str(text).split()))


def author_fields_changed(chapter, changes: dict) -> bool:
    """changes 里是否真的改动了作者字段（None 视为未提供，与仓储 update 语义一致）。"""
    for field in AUTHOR_FIELDS:
        if field not in changes:
            continue
        new_value = changes[field]
        if new_value is None:
            continue
        if new_value != getattr(chapter, field):
            return True
    return False


def snapshot_chapter(db: Session, chapter, source: str):
    """把章节当前状态加入当前事务，不提交；返回快照对象。"""
    return ChapterRevisionRepo(db).add_snapshot(
        chapter, source=source, word_count=count_words(chapter.content)
    )


def update_chapter_with_revision(
    db: Session, novel_id: str, chapter_id: str, changes: dict, source: str
):
    """原子更新章节：作者字段有实际变化时先保存覆盖前快照，再一起提交。"""
    chapter = ChapterRepo(db).get(novel_id, chapter_id)  # 不存在时抛 404，不写任何数据

    try:
        if author_fields_changed(chapter, changes):
            snapshot_chapter(db, chapter, source)
        for key, value in changes.items():
            if value is None:
                continue  # 与 ChapterRepo.update 一致：None 表示不修改
            setattr(chapter, key, value)
        db.commit()
        db.refresh(chapter)
        return chapter
    except Exception:
        db.rollback()
        raise


def delete_chapter_memory(db: Session, novel_id: str, chapter_id: str) -> int:
    """删除该章全部旧记忆切片，避免恢复后的召回命中已被替换掉的正文。"""
    return (
        db.query(models.MemoryChunk)
        .filter(
            models.MemoryChunk.novel_id == novel_id,
            models.MemoryChunk.source_chapter_id == chapter_id,
        )
        .delete(synchronize_session=False)
    )


def restore_chapter_revision(db: Session, novel_id: str, chapter_id: str, revision_id: str):
    """恢复指定版本：先快照当前状态，再回写作者字段并让派生数据失效。

    不修改 seq、outline_node_id、Story Bible、伏笔、对话或大纲。
    目标版本作者字段与当前完全相同时不制造重复快照，但仍执行恢复后的
    派生数据失效处理，避免保留与恢复动作语义冲突的定稿状态和旧记忆。
    """
    repo = ChapterRepo(db)
    chapter = repo.get(novel_id, chapter_id)  # 章节不属于该作品时抛 404
    revision = ChapterRevisionRepo(db).get(novel_id, chapter_id, revision_id)  # 跨作用域 404

    target = {
        "title": revision.title,
        "content": revision.content,
        "directive": revision.directive,
    }
    changed = author_fields_changed(chapter, target)

    try:
        # 作者字段确有变化时，先保存当前状态（来源 restore），作者才能再退回去。
        # 字段相同时不制造重复快照，但恢复语义仍要求派生数据和旧记忆失效。
        if changed:
            snapshot_chapter(db, chapter, SOURCE_RESTORE)

        chapter.title = target["title"] or ""
        chapter.content = target["content"] or ""
        chapter.directive = target["directive"] or ""
        # 派生数据与旧正文不再匹配，强制失效
        chapter.status = ChapterStatus.DRAFT.value
        chapter.summary = ""
        chapter.tension_score = None
        chapter.drift_score = None

        delete_chapter_memory(db, novel_id, chapter_id)

        db.commit()
        db.refresh(chapter)
        return chapter
    except Exception:
        db.rollback()
        raise
