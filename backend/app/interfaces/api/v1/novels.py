"""作品（Novel）路由。"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.infrastructure.db import get_db
from app.infrastructure.repository import NovelRepo
from app.infrastructure.autopilot_drafts import draft_write_lock
from app.application import auto_pilot
from app.interfaces import schemas

router = APIRouter(prefix="/novels", tags=["novel"])


@router.post("", response_model=schemas.NovelRead, status_code=201)
def create_novel(payload: schemas.NovelCreate, db: Session = Depends(get_db)):
    return NovelRepo(db).create(**payload.model_dump())


@router.get("", response_model=list[schemas.NovelRead])
def list_novels(db: Session = Depends(get_db)):
    return NovelRepo(db).list()


@router.get("/{novel_id}", response_model=schemas.NovelRead)
def get_novel(novel_id: str, db: Session = Depends(get_db)):
    return NovelRepo(db).get(novel_id)


@router.put("/{novel_id}", response_model=schemas.NovelRead)
def update_novel(novel_id: str, payload: schemas.NovelUpdate, db: Session = Depends(get_db)):
    return NovelRepo(db).update(novel_id, **payload.model_dump(exclude_unset=True))


@router.delete("/{novel_id}", status_code=204)
def delete_novel(novel_id: str, db: Session = Depends(get_db)):
    # 后台写作不会随页面退出停止；删除前避免仍在生成的线程写入孤儿正文。
    with draft_write_lock:
        if auto_pilot.registry.is_running(novel_id):
            return JSONResponse(status_code=409, content={
                "detail": "该作品的自动驾驶任务仍在运行，请等待任务结束后再删除作品",
                "code": "autopilot_busy",
            })
        NovelRepo(db).delete(novel_id)
