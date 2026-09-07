"""关系（Relationship）路由：仅建立 / 列出 / 删除。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.infrastructure.db import get_db
from app.infrastructure.repository import RelationshipRepo
from app.interfaces import schemas

router = APIRouter(prefix="/novels/{novel_id}/relationships", tags=["relationship"])


@router.post("", response_model=schemas.RelationshipRead, status_code=201)
def create_relationship(
    novel_id: str, payload: schemas.RelationshipCreate, db: Session = Depends(get_db)
):
    return RelationshipRepo(db).create(novel_id, **payload.model_dump())


@router.get("", response_model=list[schemas.RelationshipRead])
def list_relationships(novel_id: str, db: Session = Depends(get_db)):
    return RelationshipRepo(db).list(novel_id)


@router.delete("/{relationship_id}", status_code=204)
def delete_relationship(novel_id: str, relationship_id: str, db: Session = Depends(get_db)):
    RelationshipRepo(db).delete(novel_id, relationship_id)
