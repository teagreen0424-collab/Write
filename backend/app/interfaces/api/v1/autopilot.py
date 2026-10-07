"""任务启动与监控分离；订阅断开不回收后台生成任务。"""
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute

from app.application import autopilot_tasks
from app.interfaces import schemas


class AutopilotRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request: Request):
            try:
                return await handler(request)
            except autopilot_tasks.TaskStorageError as error:
                body = {"detail": error.detail, "code": error.code}
                if error.task_id:
                    body["task_id"] = error.task_id
                return JSONResponse(status_code=error.status_code, content=body)
            except RequestValidationError:
                return JSONResponse(status_code=422, content={
                    "detail": "自动驾驶任务参数不合法", "code": "autopilot_invalid",
                })

        return handle


router = APIRouter(
    prefix="/novels/{novel_id}/autopilot", tags=["autopilot"], route_class=AutopilotRoute,
)


@router.post("/start")
def start(novel_id: str, payload: schemas.AutopilotTaskStartRequest):
    word_limits = payload.word_limits.model_dump() if payload.word_limits is not None else None
    return autopilot_tasks.manager.start(novel_id, str(payload.request_id), word_limits)


def _events(novel_id: str, task_id: str, after: int | None):
    # 在开始流式响应前验证身份与游标，错误仍以普通 JSON 返回。
    snapshot = autopilot_tasks.manager.get(novel_id, task_id)
    if after is not None and after > snapshot["event_id"]:
        raise autopilot_tasks.TaskStorageError(
            409, "事件游标超过已保存的任务进度，请重新获取任务快照", "autopilot_cursor_ahead", task_id,
        )
    return StreamingResponse(
        autopilot_tasks.manager.subscribe(novel_id, task_id, after),
        media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/tasks/{task_id}/events")
def events(novel_id: str, task_id: UUID, after: int | None = Query(default=None, ge=0)):
    return _events(novel_id, str(task_id), after)


@router.get("/sse")
def autopilot_sse(novel_id: str, task_id: UUID | None = None,
                  after: int | None = Query(default=None, ge=0)):
    """兼容旧监控地址，GET 只观察已有任务，绝不启动生成。"""
    if task_id is None:
        latest = autopilot_tasks.manager.status(novel_id)
        if latest is None:
            raise autopilot_tasks.TaskStorageError(
                404, "当前没有可订阅的自动驾驶任务，请先启动", "autopilot_task_not_found",
            )
        task_id = latest["task_id"]
    return _events(novel_id, str(task_id), after)


@router.post("/tasks/{task_id}/pause")
def pause_task(novel_id: str, task_id: UUID):
    return autopilot_tasks.manager.pause(novel_id, str(task_id))


@router.post("/tasks/{task_id}/resume")
def resume_task(novel_id: str, task_id: UUID):
    return autopilot_tasks.manager.resume(novel_id, str(task_id))


@router.post("/tasks/{task_id}/inject")
def inject_task(novel_id: str, task_id: UUID, payload: schemas.AutopilotInjectRequest):
    return autopilot_tasks.manager.inject(novel_id, str(task_id), payload.directive, payload.request_id)


@router.get("/tasks/{task_id}/injections/{request_id}")
def injection_receipt(novel_id: str, task_id: UUID, request_id: UUID):
    return autopilot_tasks.manager.injection_receipt(novel_id, str(task_id), str(request_id))


@router.post("/pause")
def pause(novel_id: str, task_id: UUID = Query()):
    return pause_task(novel_id, task_id)


@router.post("/resume")
def resume(novel_id: str, task_id: UUID = Query()):
    return resume_task(novel_id, task_id)


@router.post("/inject")
def inject(novel_id: str, payload: schemas.AutopilotInjectRequest, task_id: UUID = Query()):
    return inject_task(novel_id, task_id, payload)


@router.get("/status")
def status(novel_id: str):
    return autopilot_tasks.manager.status(novel_id) or {"running": False}
