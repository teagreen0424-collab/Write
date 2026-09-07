"""自动驾驶路由：SSE + 暂停/恢复/注入/状态。"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.application import auto_pilot
from app.interfaces import schemas

router = APIRouter(prefix="/novels/{novel_id}/autopilot", tags=["autopilot"])


@router.get("/sse")
def autopilot_sse(novel_id: str):
    if auto_pilot.registry.is_running(novel_id):
        raise HTTPException(status_code=409, detail="该作品已有自动驾驶任务在运行")

    def gen():
        yield from auto_pilot.run_autopilot(novel_id)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/pause")
def pause(novel_id: str):
    state = auto_pilot.registry.pause(novel_id)
    if not state:
        raise HTTPException(status_code=404, detail="当前没有运行中的自动驾驶任务")
    return {"status": "paused"}


@router.post("/resume")
def resume(novel_id: str):
    state = auto_pilot.registry.resume(novel_id)
    if not state:
        raise HTTPException(status_code=404, detail="当前没有运行中的自动驾驶任务")
    return {"status": "resumed"}


@router.post("/inject")
def inject(novel_id: str, payload: schemas.AutopilotInjectRequest):
    state = auto_pilot.registry.inject(novel_id, payload.directive)
    if not state:
        raise HTTPException(status_code=404, detail="当前没有运行中的自动驾驶任务")
    return {"status": "injected"}


@router.get("/status")
def status(novel_id: str):
    data = auto_pilot.get_status(novel_id)
    if not data:
        return {"running": False}
    return data
