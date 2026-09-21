"""对话式构思注入路由。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.application import inject_idea as inject_uc
from app.infrastructure.db import get_db
from app.interfaces import schemas

router = APIRouter(prefix="/novels/{novel_id}/ideas", tags=["idea"])


@router.post("/chat")
def chat(novel_id: str, payload: schemas.IdeaChatRequest, db: Session = Depends(get_db)):
    return inject_uc.inject_idea(db, novel_id, payload.text)


@router.post("/apply")
def apply(novel_id: str, payload: schemas.IdeaApplyRequest, db: Session = Depends(get_db)):
    return inject_uc.apply_injections(
        db,
        novel_id,
        payload.suggestions,
        raw_text=payload.raw_text or payload.text,
        channel=payload.channel,
    )
