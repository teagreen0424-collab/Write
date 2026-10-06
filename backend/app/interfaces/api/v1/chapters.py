"""章节 CRUD + 写作/续写/改写/定稿/版本 路由。"""
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.application import revisions as revisions_uc
from app.application import settle as settle_uc
from app.application import writing as writing_uc
from app.application import outline_check as outline_check_uc
from app.application import story_review as story_review_uc
from app.application import chapter_repair as chapter_repair_uc
from app.application.word_count import check_word_limits
from app.infrastructure.db import SessionLocal, get_db
from app.infrastructure.repository import ChapterRepo, ChapterRevisionRepo
from app.interfaces import schemas

router = APIRouter(prefix="/novels/{novel_id}/chapters", tags=["chapter"])


# ================= CRUD =================
@router.get("", response_model=list[schemas.ChapterRead])
def list_chapters(novel_id: str, db: Session = Depends(get_db)):
    return ChapterRepo(db).list(novel_id)


@router.post("", response_model=schemas.ChapterRead, status_code=201)
def create_chapter(novel_id: str, payload: schemas.ChapterCreate, db: Session = Depends(get_db)):
    return ChapterRepo(db).create(novel_id, **payload.model_dump(mode="json"))


@router.get("/{chapter_id}", response_model=schemas.ChapterRead)
def get_chapter(novel_id: str, chapter_id: str, db: Session = Depends(get_db)):
    return ChapterRepo(db).get(novel_id, chapter_id)


@router.put("/{chapter_id}", response_model=schemas.ChapterRead)
def update_chapter(
    novel_id: str, chapter_id: str, payload: schemas.ChapterUpdate, db: Session = Depends(get_db)
):
    """更新章节：作者字段实际变化时先保存覆盖前快照，与更新同一事务提交。"""
    changes = payload.model_dump(exclude_unset=True, mode="json")
    # revision_source 只用于编排快照来源，必须从章节更新字段中剔除，禁止传给 ORM。
    source = changes.pop("revision_source", None) or revisions_uc.SOURCE_MANUAL_SAVE
    return revisions_uc.update_chapter_with_revision(db, novel_id, chapter_id, changes, source)


@router.delete("/{chapter_id}", status_code=204)
def delete_chapter(novel_id: str, chapter_id: str, db: Session = Depends(get_db)):
    ChapterRepo(db).delete(novel_id, chapter_id)


# ================= 章节版本 =================
@router.get("/{chapter_id}/revisions", response_model=list[schemas.ChapterRevisionMeta])
def list_chapter_revisions(novel_id: str, chapter_id: str, db: Session = Depends(get_db)):
    """按版本号倒序列出该章历史版本，不返回完整正文。"""
    ChapterRepo(db).get(novel_id, chapter_id)  # 章节不在该作品下时 404
    return ChapterRevisionRepo(db).list_by_chapter(novel_id, chapter_id)


@router.get(
    "/{chapter_id}/revisions/{revision_id}", response_model=schemas.ChapterRevisionRead
)
def get_chapter_revision(
    novel_id: str, chapter_id: str, revision_id: str, db: Session = Depends(get_db)
):
    """读取单个历史版本的完整内容。"""
    ChapterRepo(db).get(novel_id, chapter_id)
    return ChapterRevisionRepo(db).get(novel_id, chapter_id, revision_id)


@router.post(
    "/{chapter_id}/revisions/{revision_id}/restore", response_model=schemas.ChapterRead
)
def restore_chapter_revision(
    novel_id: str, chapter_id: str, revision_id: str, db: Session = Depends(get_db)
):
    """恢复历史版本：先把当前状态另存为来源 restore 的快照，再回写作者字段。"""
    return revisions_uc.restore_chapter_revision(db, novel_id, chapter_id, revision_id)


# ================= 写作（SSE 流式） =================
def _streaming(generator_factory):
    def gen():
        db = SessionLocal()
        try:
            yield from generator_factory(db)
        finally:
            db.close()

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/write")
def write_chapter(novel_id: str, payload: schemas.WriteChapterRequest):
    return _streaming(
        lambda db: writing_uc.write_chapter(
            db,
            novel_id,
            directive=payload.directive,
            title=payload.title,
            outline_node_id=payload.outline_node_id,
            previous_chapter_id=payload.previous_chapter_id,
        )
    )


@router.post("/{chapter_id}/outline-check", response_model=schemas.OutlineCheckResult)
def check_chapter_outline(
    novel_id: str, chapter_id: str, payload: schemas.OutlineCheckRequest,
    db: Session = Depends(get_db),
):
    """作者按需检查本章与绑定大纲；单次模型调用，不落库。"""
    return outline_check_uc.check_chapter_outline(db, novel_id, chapter_id, payload.content)


@router.post("/{chapter_id}/story-check", response_model=schemas.StoryCheckResult)
def check_chapter_story(
    novel_id: str, chapter_id: str, payload: schemas.StoryCheckRequest,
    db: Session = Depends(get_db),
):
    """复核当前编辑稿的构思执行与前章承接；一次调用，不保存编辑稿或设定。"""
    with db.no_autoflush:
        chapter = ChapterRepo(db).get(novel_id, chapter_id)
        result = story_review_uc.review_story(
            db, novel_id,
            chapter.content if payload.content is None else payload.content,
            directive=chapter.directive if payload.directive is None else payload.directive,
            chapter_seq=chapter.seq, outline_node_id=chapter.outline_node_id,
            word_limits=payload.word_limits.model_dump() if payload.word_limits is not None else None,
        )
        counted = check_word_limits(
            chapter.content if payload.content is None else payload.content,
            payload.word_limits.model_dump() if payload.word_limits is not None else None,
            title=chapter.title,
        )
        result.word_count = schemas.WordCountResult(
            count=counted["word_count"], min_words=counted["min_words"],
            max_words=counted["max_words"], within_limits=counted["within_limits"],
            warnings=counted["warnings"],
        )
    result.chapter_title = chapter.title
    return result


@router.post("/{chapter_id}/repair-preview", response_model=schemas.ChapterRepairPreview)
def preview_chapter_repair(
    novel_id: str, chapter_id: str, payload: schemas.ChapterRepairPreviewRequest,
    db: Session = Depends(get_db),
):
    """基于已保存全文生成整章修订预览，不改正文、版本或设定。"""
    return chapter_repair_uc.preview_repair(db, novel_id, chapter_id, payload)


@router.post("/{chapter_id}/repair-apply", response_model=schemas.ChapterRead)
def apply_chapter_repair(
    novel_id: str, chapter_id: str, payload: schemas.ChapterRepairApplyRequest,
    db: Session = Depends(get_db),
):
    """作者确认后条件应用修订稿，保留旧版；过期预览拒绝覆盖。"""
    return chapter_repair_uc.apply_repair(db, novel_id, chapter_id, payload)


@router.post("/{chapter_id}/continue")
def continue_chapter(
    novel_id: str, chapter_id: str, payload: schemas.ContinueChapterRequest
):
    return _streaming(
        lambda db: writing_uc.continue_chapter(db, novel_id, chapter_id, payload.directive)
    )


@router.post("/{chapter_id}/rewrite")
def rewrite_passage(
    novel_id: str, chapter_id: str, payload: schemas.RewritePassageRequest
):
    return _streaming(
        lambda db: writing_uc.rewrite_passage(
            db, novel_id, chapter_id, payload.passage, payload.intent, payload.preview
        )
    )


# ================= 定稿沉淀 =================
@router.post("/{chapter_id}/settle")
def settle_chapter(novel_id: str, chapter_id: str, db: Session = Depends(get_db)):
    return settle_uc.settle_chapter(db, novel_id, chapter_id)


@router.post("/{chapter_id}/settle/apply")
def settle_apply(
    novel_id: str, chapter_id: str, payload: schemas.SettleApplyRequest, db: Session = Depends(get_db)
):
    return settle_uc.apply_suggestions(db, novel_id, payload.suggestions, chapter_id)
