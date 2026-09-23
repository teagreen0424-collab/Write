"""全书统计与导出前检查。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.application import delivery as delivery_uc
from app.infrastructure.db import get_db
from app.interfaces import schemas

router = APIRouter(prefix="/novels/{novel_id}", tags=["delivery"])


@router.get("/book/summary")
def book_summary(novel_id: str, db: Session = Depends(get_db)):
    return delivery_uc.book_summary(db, novel_id)


@router.post("/export/preflight")
def export_preflight(
    novel_id: str, payload: schemas.DeliveryRequest, db: Session = Depends(get_db)
):
    return delivery_uc.preflight(db, novel_id, payload)
