"""AI 大纲预览与原子追加：工作台、聊天共用。"""
import json

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.application.chat import build_chat_context
from app.infrastructure import models
from app.infrastructure.ai.llm_provider import LLMProvider
from app.infrastructure.ai.prompts import OUTLINE_PLAN_SYSTEM, OUTLINE_PLAN_USER
from app.infrastructure.repository import NovelRepo
from app.interfaces.schemas import OutlineVolumeDraft

llm = LLMProvider()


def preview_outline(db: Session, novel_id: str, source_text: str) -> OutlineVolumeDraft:
    """生成可编辑预览；不写大纲。"""
    NovelRepo(db).get(novel_id)
    context = build_chat_context(db, novel_id, max_chars=2500)
    raw = llm.complete(
        [
            {"role": "system", "content": OUTLINE_PLAN_SYSTEM},
            {"role": "user", "content": OUTLINE_PLAN_USER.format(
                context=context or "暂无设定", source_text=source_text
            )},
        ],
        temperature=0.4,
        max_tokens=2200,
    )
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        return OutlineVolumeDraft.model_validate(json.loads(text))
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=502, detail="模型返回的大纲格式无效，请重试") from exc


def add_outline_nodes(db: Session, novel_id: str, plan: OutlineVolumeDraft) -> list[models.OutlineNode]:
    """仅加入当前事务，不自行 commit；调用方可与动作记录原子提交。"""
    NovelRepo(db).get(novel_id)
    max_order = db.query(func.max(models.OutlineNode.sort_order)).filter(
        models.OutlineNode.novel_id == novel_id,
        models.OutlineNode.parent_id == "",
    ).scalar()
    volume = models.OutlineNode(
        novel_id=novel_id, level="volume", title=plan.title.strip(),
        summary=plan.summary, parent_id="", sort_order=(max_order or 0) + 1,
    )
    db.add(volume)
    db.flush()
    nodes = [volume]
    for order, chapter in enumerate(plan.chapters, 1):
        node = models.OutlineNode(
            novel_id=novel_id, level="chapter", title=chapter.title.strip(),
            summary=chapter.summary, parent_id=volume.id, sort_order=order,
        )
        db.add(node)
        nodes.append(node)
    for order, act in enumerate(plan.acts, len(plan.chapters) + 1):
        act_node = models.OutlineNode(
            novel_id=novel_id, level="act", title=act.title.strip(),
            summary=act.summary, parent_id=volume.id, sort_order=order,
        )
        db.add(act_node)
        db.flush()
        nodes.append(act_node)
        for chapter_order, chapter in enumerate(act.chapters, 1):
            node = models.OutlineNode(
                novel_id=novel_id, level="chapter", title=chapter.title.strip(),
                summary=chapter.summary, parent_id=act_node.id,
                sort_order=chapter_order,
            )
            db.add(node)
            nodes.append(node)
    db.flush()
    return nodes


def apply_outline(db: Session, novel_id: str, plan: OutlineVolumeDraft) -> dict:
    """工作台确认：新增一卷与子节点，事务失败不留半棵树。"""
    try:
        nodes = add_outline_nodes(db, novel_id, plan)
        result = {"volume_id": nodes[0].id, "node_ids": [n.id for n in nodes]}
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
