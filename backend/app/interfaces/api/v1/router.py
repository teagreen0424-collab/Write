"""v1 路由聚合。"""
from fastapi import APIRouter

from app.infrastructure import repository as repos
from app.interfaces import schemas
from app.interfaces.api.v1 import chapters, consistency, export, health, novels, relationships
from app.interfaces.api.v1.crud import build_scoped_crud_router

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(novels.router)
api_router.include_router(relationships.router)
api_router.include_router(chapters.router)
api_router.include_router(consistency.router)
api_router.include_router(export.router)

# 六类设定实体：作品作用域下的 CRUD
api_router.include_router(
    build_scoped_crud_router(
        "characters", repos.CharacterRepo,
        schemas.CharacterCreate, schemas.CharacterUpdate, schemas.CharacterRead,
    )
)
api_router.include_router(
    build_scoped_crud_router(
        "locations", repos.LocationRepo,
        schemas.LocationCreate, schemas.LocationUpdate, schemas.LocationRead,
    )
)
api_router.include_router(
    build_scoped_crud_router(
        "world-settings", repos.WorldSettingRepo,
        schemas.WorldSettingCreate, schemas.WorldSettingUpdate, schemas.WorldSettingRead,
    )
)
api_router.include_router(
    build_scoped_crud_router(
        "timeline-events", repos.TimelineEventRepo,
        schemas.TimelineEventCreate, schemas.TimelineEventUpdate, schemas.TimelineEventRead,
    )
)
api_router.include_router(
    build_scoped_crud_router(
        "foreshadowings", repos.ForeshadowingRepo,
        schemas.ForeshadowingCreate, schemas.ForeshadowingUpdate, schemas.ForeshadowingRead,
    )
)
