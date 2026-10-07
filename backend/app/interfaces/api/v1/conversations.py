"""全局对话路由（SSE 流式）。"""
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.application import chat as chat_uc
from app.application import chat_actions
from app.infrastructure.db import SessionLocal, get_db
from app.infrastructure.repository import ConversationRepo
from app.interfaces import schemas

router = APIRouter(prefix="/conversations", tags=["conversation"])


@router.get("/{conversation_id}/messages/{message_id}/actions")
def list_message_actions(conversation_id: str, message_id: str, db: Session = Depends(get_db)):
    return chat_actions.list_actions(db, conversation_id, message_id)


@router.post("/{conversation_id}/messages/{message_id}/settings/preview")
def preview_message_settings(conversation_id: str, message_id: str, db: Session = Depends(get_db)):
    return chat_actions.preview_settings(db, conversation_id, message_id)


@router.post("/{conversation_id}/messages/{message_id}/settings/apply")
def apply_message_settings(
    conversation_id: str, message_id: str, payload: schemas.IdeaApplyRequest,
    db: Session = Depends(get_db),
):
    return chat_actions.apply_settings(db, conversation_id, message_id, payload.suggestions)


@router.post("/{conversation_id}/messages/{message_id}/outline/preview", response_model=schemas.OutlineVolumeDraft)
def preview_message_outline(conversation_id: str, message_id: str, db: Session = Depends(get_db)):
    return chat_actions.preview_outline(db, conversation_id, message_id)


@router.post("/{conversation_id}/messages/{message_id}/outline/apply")
def apply_message_outline(
    conversation_id: str, message_id: str, payload: schemas.OutlineApplyRequest,
    db: Session = Depends(get_db),
):
    return chat_actions.apply_outline(db, conversation_id, message_id, payload.plan)


@router.post("/{conversation_id}/messages/{message_id}/chapter/write")
def write_from_message(
    conversation_id: str, message_id: str, payload: schemas.WriteChapterRequest,
):
    return _streaming(lambda db: chat_actions.write_chapter(db, conversation_id, message_id, payload))


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


@router.get("/{conversation_id}/messages/requests/{request_id}")
def get_message_receipt(conversation_id: str, request_id: UUID, db: Session = Depends(get_db)):
    try:
        return chat_uc.get_message_receipt(db, conversation_id, request_id)
    except chat_uc.ChatMessageRequestError as error:
        return JSONResponse(status_code=error.status_code, content={"detail": error.detail, "code": error.code})


@router.patch("/{conversation_id}/messages/{message_id}", response_model=schemas.MessageRead)
def update_message(
    conversation_id: str, message_id: str, payload: schemas.MessageUpdate, db: Session = Depends(get_db)
):
    return ConversationRepo(db).update_message(conversation_id, message_id, meta=payload.meta)


@router.post("/{conversation_id}/messages")
def send_message(conversation_id: str, payload: schemas.MessageCreate):
    return _streaming(
        lambda db: chat_uc.reply_conversation(db, conversation_id, payload.content, request_id=payload.request_id)
    )


@router.post("/{conversation_id}/promote", response_model=schemas.NovelRead, status_code=201)
def promote_conversation(
    conversation_id: str, payload: schemas.PromoteRequest, db: Session = Depends(get_db)
):
    return chat_uc.promote_conversation(
        db, conversation_id, payload.title, payload.genre, payload.summary
    )
