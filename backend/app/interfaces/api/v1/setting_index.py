"""设定向量索引维护入口。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.application.setting_index import rebuild_settings_index
from app.infrastructure.db import get_db

router = APIRouter(prefix="/novels/{novel_id}/memory", tags=["memory"])


@router.post("/rebuild-settings")
def rebuild_settings(novel_id: str, db: Session = Depends(get_db)):
    return rebuild_settings_index(db, novel_id)
