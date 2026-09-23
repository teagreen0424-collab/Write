"""导出路由。"""
from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse, Response
from urllib.parse import quote
from sqlalchemy.orm import Session

from app.application import export as export_uc
from app.infrastructure.db import get_db
from app.infrastructure.repository import NovelRepo
from app.interfaces import schemas

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


@router.post("/file")
def export_file(
    novel_id: str, payload: schemas.DeliveryRequest, db: Session = Depends(get_db)
):
    content = export_uc.export_file(db, novel_id, payload)
    mime = {
        "markdown": "text/markdown; charset=utf-8",
        "txt": "text/plain; charset=utf-8",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }[payload.format]
    title = NovelRepo(db).get(novel_id).title
    extension = {"markdown": "md", "txt": "txt", "docx": "docx"}[payload.format]
    filename = quote(f"{title}.{extension}", safe="")
    return Response(
        content=content,
        media_type=mime,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )
