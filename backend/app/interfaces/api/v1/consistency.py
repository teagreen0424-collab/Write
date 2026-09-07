"""一致性检查路由。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.application import consistency as consistency_uc
from app.infrastructure.db import get_db
from app.interfaces import schemas

router = APIRouter(prefix="/novels/{novel_id}", tags=["consistency"])


@router.post("/consistency")
def check_consistency(
    novel_id: str, payload: schemas.ConsistencyRequest, db: Session = Depends(get_db)
):
    return consistency_uc.consistency_check(db, novel_id, payload.content)
