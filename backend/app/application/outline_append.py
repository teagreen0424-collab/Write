"""工作台大纲追加的请求身份；原节点与回执同一短事务提交。"""
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.application.outline_plan import add_outline_nodes
from app.application.revisions import _begin_chapter_write, _require_clean_session
from app.infrastructure import models
from app.infrastructure.repository import NovelRepo
from app.interfaces.schemas import OutlineVolumeDraft


class OutlineAppendError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = 409


def _receipt(db: Session, request_id: str):
    table = models.OutlineAppendReceipt.__table__
    return db.execute(select(table).where(table.c.request_id == request_id)).mappings().first()


def _replay(db: Session, novel_id: str, receipt, fingerprint: str) -> dict:
    if receipt['novel_id'] != novel_id:
        raise OutlineAppendError('outline_append_scope_conflict', '该追加请求已用于另一作品，请核对原追加结果')
    if receipt['payload_fingerprint'] != fingerprint:
        raise OutlineAppendError('outline_append_payload_conflict', '该请求已提交不同的大纲，请使用原提交核对结果')
    table = models.OutlineNode.__table__
    existing_ids = set(db.execute(select(table.c.id).where(
        table.c.novel_id == novel_id, table.c.id.in_(receipt['node_ids']),
    )).scalars())
    if existing_ids != set(receipt['node_ids']):
        raise OutlineAppendError('outline_append_deleted', '原追加的节点已有删除，不会重新创建或补回，请查看当前大纲')
    return {'volume_id': receipt['volume_id'], 'node_ids': list(receipt['node_ids']),
            'request_id': receipt['request_id'], 'append_replayed': True}


def append_outline_once(db: Session, novel_id: str, plan: OutlineVolumeDraft, request_id: str) -> dict:
    # 拒绝调用方未提交写入不能回滚其既有事务；无UUID旧路径仍由outline_plan保持。
    _require_clean_session(db)
    canonical = json.dumps({'version': 1, 'novel_id': novel_id, 'plan': plan.model_dump(mode='json')},
                           ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    fingerprint = hashlib.sha256(canonical.encode('utf8')).hexdigest()
    try:
        NovelRepo(db).get(novel_id)
        previous = _receipt(db, request_id)
        if previous is not None:
            return _replay(db, novel_id, previous, fingerprint)
        _begin_chapter_write(db)
        NovelRepo(db).get(novel_id)
        previous = _receipt(db, request_id)
        if previous is not None:
            result = _replay(db, novel_id, previous, fingerprint)
            db.rollback()
            return result
        nodes = add_outline_nodes(db, novel_id, plan)
        node_ids = [node.id for node in nodes]
        db.add(models.OutlineAppendReceipt(request_id=request_id, novel_id=novel_id,
            volume_id=node_ids[0], node_ids=node_ids, payload_fingerprint=fingerprint))
        db.flush()
        result = {'volume_id': node_ids[0], 'node_ids': node_ids,
                  'request_id': request_id, 'append_replayed': False}
        db.commit()
        return result  # 提交后的返回只读已冻结事实，不再查库。
    except OperationalError:
        db.rollback()
        raise OutlineAppendError('outline_append_busy', '追加结果尚未确认，请保留同一请求和原大纲核对') from None
    except Exception:
        db.rollback()
        raise
