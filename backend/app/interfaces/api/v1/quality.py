"""质量治理路由：去 AI 味扫描、质量曲线、伏笔追踪聚合。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.application import cliche as cliche_uc
from app.application import quality as quality_uc
from app.infrastructure.db import get_db
from app.interfaces import schemas

router = APIRouter(prefix="/novels/{novel_id}", tags=["quality"])


@router.post("/quality/cliche", response_model=schemas.ClicheResponse)
def scan_cliche(novel_id: str, payload: schemas.ClicheRequest):
    """扫描套路表达，返回命中项、每千字密度与四类分布。"""
    return cliche_uc.scan_cliches(payload.content)


@router.get("/quality/curve", response_model=schemas.QualityCurveResponse)
def get_quality_curve(novel_id: str, db: Session = Depends(get_db)):
    """全书质量曲线（张力分 + 漂移分）与文风基准状态。"""
    return quality_uc.build_curve(db, novel_id)


@router.get("/quality/foreshadowings", response_model=schemas.ForeshadowingTrackingResponse)
def get_foreshadowings(novel_id: str, db: Session = Depends(get_db)):
    """伏笔追踪聚合，按状态分组并派生长悬置标记。"""
    return quality_uc.aggregate_foreshadowings(db, novel_id)
