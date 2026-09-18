"""章节 CRUD + 写作/续写/改写/定稿 路由。"""
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.application import settle as settle_uc
from app.application import writing as writing_uc
from app.infrastructure.db import SessionLocal, get_db
from app.infrastructure.repository import ChapterRepo
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
    return ChapterRepo(db).update(
        novel_id, chapter_id, **payload.model_dump(exclude_unset=True, mode="json")
    )


@router.delete("/{chapter_id}", status_code=204)
def delete_chapter(novel_id: str, chapter_id: str, db: Session = Depends(get_db)):
    ChapterRepo(db).delete(novel_id, chapter_id)


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
    return _streaming(lambda db: writing_uc.write_chapter(db, novel_id, payload.directive, payload.title))


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
