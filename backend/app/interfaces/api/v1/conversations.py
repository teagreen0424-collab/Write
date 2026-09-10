"""全局对话路由（SSE 流式）。"""
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.application import chat as chat_uc
from app.infrastructure.db import SessionLocal, get_db
from app.infrastructure.repository import ConversationRepo
from app.interfaces import schemas

router = APIRouter(prefix="/conversations", tags=["conversation"])


def _streaming(generator_factory):
    def gen():
        db = SessionLocal()
        try:
            yield from generator_factory(db)
        finally:
            db.close()

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("", response_model=list[schemas.ConversationRead])
def list_conversations(db: Session = Depends(get_db)):
    return ConversationRepo(db).list()


@router.post("", response_model=schemas.ConversationRead, status_code=201)
def create_conversation(payload: schemas.ConversationCreate, db: Session = Depends(get_db)):
    return ConversationRepo(db).create(novel_id=payload.novel_id)


@router.patch("/{conversation_id}", response_model=schemas.ConversationRead)
def update_conversation(
    conversation_id: str, payload: schemas.ConversationUpdate, db: Session = Depends(get_db)
):
    return ConversationRepo(db).update(
        conversation_id, **payload.model_dump(exclude_unset=True)
    )


@router.delete("/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: str, db: Session = Depends(get_db)):
    ConversationRepo(db).delete(conversation_id)


@router.get("/{conversation_id}/messages", response_model=list[schemas.MessageRead])
def list_messages(conversation_id: str, db: Session = Depends(get_db)):
    return ConversationRepo(db).list_messages(conversation_id)


@router.patch("/{conversation_id}/messages/{message_id}", response_model=schemas.MessageRead)
def update_message(
    conversation_id: str, message_id: str, payload: schemas.MessageUpdate, db: Session = Depends(get_db)
):
    return ConversationRepo(db).update_message(conversation_id, message_id, meta=payload.meta)


@router.post("/{conversation_id}/messages")
def send_message(conversation_id: str, payload: schemas.MessageCreate):
    return _streaming(
        lambda db: chat_uc.reply_conversation(db, conversation_id, payload.content)
    )


@router.post("/{conversation_id}/promote", response_model=schemas.NovelRead, status_code=201)
def promote_conversation(
    conversation_id: str, payload: schemas.PromoteRequest, db: Session = Depends(get_db)
):
    return chat_uc.promote_conversation(
        db, conversation_id, payload.title, payload.genre, payload.summary
    )
