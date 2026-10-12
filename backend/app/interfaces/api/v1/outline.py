"""大纲节点路由（卷/幕/章三级 + 同级重排）。"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from uuid import UUID
from sqlalchemy.orm import Session

from app.domain.entities import OutlineLevel
from app.application import outline_plan
from app.application.outline_append import OutlineAppendError, append_outline_once, check_outline_append
from app.infrastructure.db import get_db
from app.infrastructure.repository import OutlineRepo
from app.interfaces import schemas

router = APIRouter(prefix="/novels/{novel_id}/outline", tags=["outline"])


@router.post("/preview", response_model=schemas.OutlineVolumeDraft)
def preview_outline(novel_id: str, payload: schemas.OutlinePreviewRequest, db: Session = Depends(get_db)):
    return outline_plan.preview_outline(db, novel_id, payload.source_text)


@router.post("/apply")
def apply_outline(novel_id: str, payload: schemas.OutlineApplyRequest, db: Session = Depends(get_db)):
    if payload.request_id is not None:
        try:
            return append_outline_once(db, novel_id, payload.plan, str(payload.request_id))
        except OutlineAppendError as error:
            return JSONResponse(status_code=error.status_code, content={'detail': error.detail, 'code': error.code})
    return outline_plan.apply_outline(db, novel_id, payload.plan)


@router.post('/append-requests/{request_id}/check')
def check_append(novel_id: str, request_id: UUID, payload: schemas.OutlineApplyRequest, db: Session = Depends(get_db)):
    try:
        return check_outline_append(db, novel_id, payload.plan, str(request_id))
    except OutlineAppendError as error:
        return JSONResponse(status_code=error.status_code, content={'detail': error.detail, 'code': error.code})


def _validate_parent(db: Session, novel_id: str, level: str, parent_id: str) -> None:
    """校验层级合法性：卷无父、幕挂卷、章挂卷/幕，父节点须存在且同属作品。"""
    if level == OutlineLevel.VOLUME.value:
        if parent_id:
            raise HTTPException(status_code=400, detail="卷节点不能有父节点")
        return
    if not parent_id:
        raise HTTPException(status_code=400, detail="幕/章节点必须挂在卷或幕下")

    parent = OutlineRepo(db).get(novel_id, parent_id)
    if level == OutlineLevel.ACT.value and parent.level != OutlineLevel.VOLUME.value:
        raise HTTPException(status_code=400, detail="幕节点只能挂在卷下")
    if level == OutlineLevel.CHAPTER.value and parent.level not in (
        OutlineLevel.VOLUME.value,
        OutlineLevel.ACT.value,
    ):
        raise HTTPException(status_code=400, detail="章节点只能挂在卷或幕下")


@router.get("", response_model=list[schemas.OutlineNodeRead])
def list_outline(novel_id: str, db: Session = Depends(get_db)):
    return OutlineRepo(db).list_all(novel_id)


@router.get("/{node_id}", response_model=schemas.OutlineNodeRead)
def get_outline(novel_id: str, node_id: str, db: Session = Depends(get_db)):
    return OutlineRepo(db).get(novel_id, node_id)


@router.post("", response_model=schemas.OutlineNodeRead, status_code=201)
def create_outline(novel_id: str, payload: schemas.OutlineNodeCreate, db: Session = Depends(get_db)):
    data = payload.model_dump(mode="json")
    _validate_parent(db, novel_id, data["level"], data.get("parent_id", ""))
    return OutlineRepo(db).create(novel_id, **data)


@router.put("/{node_id}", response_model=schemas.OutlineNodeRead)
def update_outline(
    novel_id: str, node_id: str, payload: schemas.OutlineNodeUpdate, db: Session = Depends(get_db)
):
    data = payload.model_dump(exclude_unset=True, mode="json")
    # 校验层级：若修改 level 或 parent_id，按新值校验
    node = OutlineRepo(db).get(novel_id, node_id)
    level = data.get("level", node.level)
    parent_id = data.get("parent_id", node.parent_id)
    if "level" in data or "parent_id" in data:
        _validate_parent(db, novel_id, level, parent_id)
    return OutlineRepo(db).update(novel_id, node_id, **data)


@router.delete("/{node_id}", status_code=204)
def delete_outline(novel_id: str, node_id: str, db: Session = Depends(get_db)):
    OutlineRepo(db).delete(novel_id, node_id)


@router.post("/reorder", response_model=list[schemas.OutlineNodeRead])
def reorder_outline(
    novel_id: str, payload: schemas.OutlineReorderRequest, db: Session = Depends(get_db)
):
    ordered_ids = payload.ordered_ids
    nodes = [OutlineRepo(db).get(novel_id, nid) for nid in ordered_ids]
    if len({n.parent_id for n in nodes}) != 1:
        raise HTTPException(status_code=400, detail="只能对同级节点重排")
    OutlineRepo(db).reorder(novel_id, ordered_ids)
    return OutlineRepo(db).list_all(novel_id)
