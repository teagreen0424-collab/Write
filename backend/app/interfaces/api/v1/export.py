"""导出路由。"""
from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.application import export as export_uc
from app.infrastructure.db import get_db

router = APIRouter(prefix="/novels/{novel_id}/export", tags=["export"])


@router.get("/markdown")
def export_markdown(novel_id: str, db: Session = Depends(get_db)):
    return PlainTextResponse(
        export_uc.export_markdown(db, novel_id),
        media_type="text/markdown; charset=utf-8",
    )


@router.get("/txt")
def export_txt(novel_id: str, db: Session = Depends(get_db)):
    return PlainTextResponse(
        export_uc.export_txt(db, novel_id),
        media_type="text/plain; charset=utf-8",
    )
