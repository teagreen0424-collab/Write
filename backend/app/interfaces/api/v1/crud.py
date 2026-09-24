"""通用 CRUD 路由工厂：六类作品作用域实体共用。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.infrastructure.db import get_db
from app.application import setting_index


def build_scoped_crud_router(
    entity_name: str,
    repo_class,
    create_schema,
    update_schema,
    read_schema,
) -> APIRouter:
    """生成 /novels/{novel_id}/{entity_name} 下的增删改查路由。"""
    router = APIRouter(prefix=f"/novels/{{novel_id}}/{entity_name}", tags=[entity_name])

    @router.post("", response_model=read_schema, status_code=201)
    def create_item(novel_id: str, payload: create_schema, db: Session = Depends(get_db)):
        return setting_index.create_setting(db, repo_class, novel_id, **payload.model_dump(mode="json"))

    @router.get("", response_model=list[read_schema])
    def list_items(novel_id: str, db: Session = Depends(get_db)):
        return repo_class(db).list(novel_id)

    @router.get("/{item_id}", response_model=read_schema)
    def get_item(novel_id: str, item_id: str, db: Session = Depends(get_db)):
        return repo_class(db).get(novel_id, item_id)

    @router.put("/{item_id}", response_model=read_schema)
    def update_item(novel_id: str, item_id: str, payload: update_schema, db: Session = Depends(get_db)):
        return setting_index.update_setting(
            db, repo_class, novel_id, item_id, **payload.model_dump(exclude_unset=True, mode="json")
        )

    @router.delete("/{item_id}", status_code=204)
    def delete_item(novel_id: str, item_id: str, db: Session = Depends(get_db)):
        setting_index.delete_setting(db, repo_class, novel_id, item_id)

    return router
