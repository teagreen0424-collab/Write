"""作品（Novel）路由。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.infrastructure.db import get_db
from app.infrastructure.repository import NovelRepo
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
    NovelRepo(db).delete(novel_id)
