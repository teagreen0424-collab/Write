"""章节版本用例：覆盖前原子快照、安全恢复与派生数据失效。

版本只覆盖三个作者字段：title / content / directive。
status、summary、tension_score、drift_score 等派生状态只随快照留档供审阅，不参与「是否变化」的判断。

事务约定（不许绕过）：
- 快照插入与章节更新必须由同一次 `db.commit()` 完成；
- 禁止「先调用一个会 commit 的仓储方法写正文，再补写版本」这种伪原子写法；
- 例外：快照本身没有需要回滚的后续动作时，只插入不加其它写入，此时单独提交是安全的。
"""
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

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


def _require_clean_session(db: Session):
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("章节写入前不能存在未提交修改")
    if db.in_transaction() and db.get_bind().dialect.name == "sqlite":
        # flush/Core DML之后三个ORM集合可能都为空；驱动事务仍包含调用方的未提交写入。
        driver = db.connection().connection.driver_connection
        if driver.in_transaction:
            raise RuntimeError("章节写入前不能存在已执行但未提交的写事务")


def _begin_chapter_write(db: Session):
    """仅在主写入阶段串行读取、版本分配与更新，不锁模型等待。"""
    db.rollback()
    if db.get_bind().dialect.name == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))
    db.expire_all()


def _invalidate_derived(db: Session, chapter):
    chapter.status = ChapterStatus.DRAFT.value
    chapter.summary = ""
    chapter.tension_score = None
    chapter.drift_score = None
    delete_chapter_memory(db, chapter.novel_id, chapter.id)


def _freeze_chapter(db: Session, chapter) -> dict:
    """提交前取全列结果，包含flush生成的updated_at，提交后响应无需再查库。"""
    db.flush()
    return {column.key: getattr(chapter, column.key) for column in models.Chapter.__table__.columns}


def _committed_chapter(chapter, frozen: dict):
    """保持同一ORM身份，恢复已提交列而不触发SQL或制造待提交修改。"""
    for key, value in frozen.items():
        set_committed_value(chapter, key, value)
    return chapter


def update_chapter_with_revision(
    db: Session, novel_id: str, chapter_id: str, changes: dict, source: str,
    *, expected_author_fields: dict | None = None,
):
    """原子更新章节：作者字段有实际变化时先保存覆盖前快照，再一起提交。"""
    # 在事务try之外拒绝，避免rollback丢弃调用方有意的额外未提交写入。
    _require_clean_session(db)
    try:
        _begin_chapter_write(db)
        chapter = ChapterRepo(db).get(novel_id, chapter_id)  # 重读已提交稿件，快照不能来自旧ORM缓存
        if expected_author_fields is not None and any(
            getattr(chapter, field) != expected_author_fields[field] for field in AUTHOR_FIELDS
        ):
            raise HTTPException(status_code=409, detail="生成期间章节已变化；当前稿未被覆盖，请核对最新稿后重试")
        changed = author_fields_changed(chapter, changes)
        if changed:
            snapshot_chapter(db, chapter, source)
        for key, value in changes.items():
            if value is None:
                continue  # 与 ChapterRepo.update 一致：None 表示不修改
            setattr(chapter, key, value)
        if changed:
            # 作者字段变化时，随请求附带的旧status/summary不能让新稿继续冒充已复核正文。
            _invalidate_derived(db, chapter)
        frozen = _freeze_chapter(db, chapter)
        db.commit()
    except OperationalError:
        db.rollback()
        raise HTTPException(status_code=409, detail="章节正在保存，请稍后重试；当前稿未被覆盖") from None
    except Exception:
        db.rollback()
        raise
    return _committed_chapter(chapter, frozen)


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
    _require_clean_session(db)
    try:
        _begin_chapter_write(db)
        chapter = ChapterRepo(db).get(novel_id, chapter_id)
        revision = ChapterRevisionRepo(db).get(novel_id, chapter_id, revision_id)
        target = {field: getattr(revision, field) for field in AUTHOR_FIELDS}
        changed = author_fields_changed(chapter, target)
        # 作者字段确有变化时，先保存当前状态（来源 restore），作者才能再退回去。
        # 字段相同时不制造重复快照，但恢复语义仍要求派生数据和旧记忆失效。
        if changed:
            snapshot_chapter(db, chapter, SOURCE_RESTORE)

        chapter.title = target["title"] or ""
        chapter.content = target["content"] or ""
        chapter.directive = target["directive"] or ""
        # 派生数据与旧正文不再匹配，强制失效
        _invalidate_derived(db, chapter)

        frozen = _freeze_chapter(db, chapter)
        db.commit()
    except OperationalError:
        db.rollback()
        raise HTTPException(status_code=409, detail="章节正在保存，请稍后重试；当前稿未被恢复") from None
    except Exception:
        db.rollback()
        raise
    return _committed_chapter(chapter, frozen)
