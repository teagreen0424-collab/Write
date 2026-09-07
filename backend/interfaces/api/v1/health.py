"""健康检查路由。"""
from fastapi import APIRouter, Depends

from config import Settings, get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health(settings: Settings = Depends(get_settings)) -> dict:
    """返回服务健康状态。"""
    return {
        "status": "ok",
        "app": "lingmo-writer",
        "version": "0.1.0",
    }
