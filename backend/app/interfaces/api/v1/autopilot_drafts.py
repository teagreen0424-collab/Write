"""自动驾驶恢复草稿 API；正文保存和设定沉淀均不经过这里。"""
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from app.infrastructure.autopilot_drafts import AutopilotDraftRepo, DraftStorageError, draft_write_lock
from app.infrastructure.db import get_db
from app.interfaces import schemas


class DraftRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request: Request):
            try:
                return await handler(request)
            except DraftStorageError as error:
                return JSONResponse(status_code=error.status_code, content={
                    "detail": error.detail, "code": error.code,
                })
            except RequestValidationError:
                return JSONResponse(status_code=422, content={
                    "detail": "草稿数据不合法", "code": "draft_invalid",
                })

        return handle


router = APIRouter(
    prefix="/novels/{novel_id}/autopilot/drafts", tags=["autopilot"], route_class=DraftRoute,
)


@router.get("", response_model=list[schemas.AutopilotDraftRead])
def list_drafts(novel_id: str, db: Session = Depends(get_db)):
    return AutopilotDraftRepo(db).list(novel_id)


@router.put("/{draft_id}", response_model=schemas.AutopilotDraftRead)
def upsert_draft(novel_id: str, draft_id: UUID, payload: schemas.AutopilotDraftUpsert,
                 db: Session = Depends(get_db)):
    from app.application.auto_pilot import registry

    with draft_write_lock:
        repo = AutopilotDraftRepo(db)
        obj = repo.get_raw(novel_id, str(draft_id))
        if obj is not None and not obj.deleted and obj.state == "active" and registry.is_running(novel_id):
            raise DraftStorageError(
                409, "该片段仍在后台生成，不能覆盖服务端已保存的进度", "draft_still_running",
            )
        return repo.upsert(novel_id, str(draft_id), **payload.model_dump())


@router.delete("/{draft_id}", response_model=schemas.AutopilotDraftDeleted)
def delete_draft(novel_id: str, draft_id: UUID,
                 revision: int | None = Query(default=None, ge=1, le=2_147_483_647),
                 db: Session = Depends(get_db)):
    from app.application.auto_pilot import registry

    with draft_write_lock:
        repo = AutopilotDraftRepo(db)
        obj = repo.get_raw(novel_id, str(draft_id))
        if obj is not None and not obj.deleted and obj.state == "active" and registry.is_running(novel_id):
            raise DraftStorageError(
                409, "该片段仍在后台生成，请等待本章结束后再放弃；可先复制", "draft_still_running",
            )
        return repo.delete(novel_id, str(draft_id), revision)
