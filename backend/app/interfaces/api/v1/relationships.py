"""关系（Relationship）路由：作品内建立、读取、修改与删除。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.infrastructure.db import get_db
from app.infrastructure.repository import RelationshipRepo
from app.application import setting_index
from app.interfaces import schemas

router = APIRouter(prefix="/novels/{novel_id}/relationships", tags=["relationship"])


@router.post("", response_model=schemas.RelationshipRead, status_code=201)
def create_relationship(
    novel_id: str, payload: schemas.RelationshipCreate, db: Session = Depends(get_db)
):
    return setting_index.create_setting(db, RelationshipRepo, novel_id, **payload.model_dump())


@router.get("", response_model=list[schemas.RelationshipRead])
def list_relationships(novel_id: str, db: Session = Depends(get_db)):
    return RelationshipRepo(db).list(novel_id)


@router.get("/{relationship_id}", response_model=schemas.RelationshipRead)
def get_relationship(novel_id: str, relationship_id: str, db: Session = Depends(get_db)):
    return RelationshipRepo(db).get(novel_id, relationship_id)


@router.put("/{relationship_id}", response_model=schemas.RelationshipRead)
def update_relationship(
    novel_id: str, relationship_id: str, payload: schemas.RelationshipUpdate,
    db: Session = Depends(get_db),
):
    return setting_index.update_setting(
        db, RelationshipRepo, novel_id, relationship_id,
        **payload.model_dump(exclude_unset=True, mode="json"),
    )


@router.delete("/{relationship_id}", status_code=204)
def delete_relationship(novel_id: str, relationship_id: str, db: Session = Depends(get_db)):
    setting_index.delete_setting(db, RelationshipRepo, novel_id, relationship_id)
