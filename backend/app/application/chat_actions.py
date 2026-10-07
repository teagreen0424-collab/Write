"""聊天来源校验与动作去重。"""
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.application import outline_plan
from app.application import writing
from app.application import inject_idea
from app.infrastructure import models
from app.infrastructure.repository import ConversationRepo
from app.interfaces.schemas import OutlineVolumeDraft
from app.interfaces.schemas import WriteChapterRequest


def source_message(db: Session, conversation_id: str, message_id: str):
    conv = ConversationRepo(db).get(conversation_id)
    if not conv.novel_id:
        raise HTTPException(status_code=400, detail="请先将会话绑定到作品")
    message = db.query(models.Message).filter(
        models.Message.id == message_id,
        models.Message.conversation_id == conversation_id,
        models.Message.role == "assistant",
    ).first()
    if not message:
        raise HTTPException(status_code=404, detail="未找到该会话中的助手消息")
    return conv, message


def list_actions(db: Session, conversation_id: str, message_id: str) -> list[dict]:
    conv, _ = source_message(db, conversation_id, message_id)
    actions = db.query(models.ChatAction).filter(
        models.ChatAction.message_id == message_id,
        models.ChatAction.novel_id == conv.novel_id,
    ).order_by(models.ChatAction.created_at).all()
    result = []
    for action in actions:
        target_deleted = False
        if action.action_type == "outline":
            target_deleted = db.query(models.OutlineNode.id).filter(
                models.OutlineNode.id == action.result_id,
                models.OutlineNode.novel_id == conv.novel_id,
            ).first() is None
        elif action.action_type == "chapter":
            target_deleted = db.query(models.Chapter.id).filter(
                models.Chapter.id == action.result_id,
                models.Chapter.novel_id == conv.novel_id,
            ).first() is None
        result.append({
            "action_type": action.action_type, "status": action.status,
            "result_id": action.result_id, "result_summary": action.result_summary,
            "target_deleted": target_deleted,
        })
    return result


def preview_outline(db: Session, conversation_id: str, message_id: str) -> OutlineVolumeDraft:
    conv, message = source_message(db, conversation_id, message_id)
    return outline_plan.preview_outline(db, conv.novel_id, message.content)


_SETTING_TYPES = {"character", "world_setting", "foreshadowing"}


def preview_settings(db: Session, conversation_id: str, message_id: str) -> dict:
    conv, message = source_message(db, conversation_id, message_id)
    result = inject_idea.inject_idea(db, conv.novel_id, message.content)
    allowed = [(index, ch) for index, ch in enumerate(result["changes"])
               if isinstance(ch, dict) and ch.get("type") in _SETTING_TYPES]
    positions = {original: index for index, (original, _) in enumerate(allowed)}
    return {
        **result,
        "changes": [ch for _, ch in allowed],
        "failed": [{**item, "index": positions[item["index"]]}
                   for item in result.get("failed", []) if item["index"] in positions],
    }


def apply_settings(db: Session, conversation_id: str, message_id: str, suggestions: list[dict]) -> dict:
    conv, message = source_message(db, conversation_id, message_id)
    if not suggestions or any(
        not isinstance(ch, dict) or ch.get("type") not in _SETTING_TYPES
        or ch.get("action", "create") not in {"create", "update"}
        for ch in suggestions
    ):
        raise HTTPException(status_code=400, detail="仅可确认非空的人物、世界观或伏笔建议")
    existing = db.query(models.ChatAction).filter(
        models.ChatAction.message_id == message_id,
        models.ChatAction.action_type == "settings",
    ).first()
    if existing:
        if existing.novel_id != conv.novel_id:
            raise HTTPException(status_code=409, detail="该动作属于其他作品")
        return existing.result_summary
    try:
        action = models.ChatAction(
            message_id=message_id, novel_id=conv.novel_id,
            action_type="settings", status="done", result_id="", result_summary={},
        )
        db.add(action)
        db.flush()
        result = inject_idea.apply_injections(
            db, conv.novel_id, suggestions, raw_text=message.content,
            channel="chat", atomic=True,
        )
        action.result_id = result["applied"][0]["id"] if result["applied"] else ""
        action.result_summary = result
        db.commit()
        return result
    except IntegrityError:
        db.rollback()
        existing = db.query(models.ChatAction).filter(
            models.ChatAction.message_id == message_id,
            models.ChatAction.action_type == "settings",
        ).first()
        if existing:
            return existing.result_summary
        raise
    except Exception:
        db.rollback()
        raise


def apply_outline(
    db: Session, conversation_id: str, message_id: str, plan: OutlineVolumeDraft,
) -> dict:
    conv, _ = source_message(db, conversation_id, message_id)
    existing = db.query(models.ChatAction).filter(
        models.ChatAction.message_id == message_id,
        models.ChatAction.action_type == "outline",
    ).first()
    if existing:
        if existing.novel_id != conv.novel_id:
            raise HTTPException(status_code=409, detail="该动作属于其他作品")
        return existing.result_summary

    try:
        action = models.ChatAction(
            message_id=message_id, novel_id=conv.novel_id, action_type="outline",
            status="done", result_id="", result_summary={},
        )
        db.add(action)
        db.flush()  # 先占唯一键；冲突时不创建任何大纲节点。
        nodes = outline_plan.add_outline_nodes(db, conv.novel_id, plan)
        result = {"volume_id": nodes[0].id, "node_ids": [n.id for n in nodes]}
        action.result_id = nodes[0].id
        action.result_summary = result
        db.commit()
        return result
    except IntegrityError:
        db.rollback()
        existing = db.query(models.ChatAction).filter(
            models.ChatAction.message_id == message_id,
            models.ChatAction.action_type == "outline",
        ).first()
        if existing:
            return existing.result_summary
        raise
    except Exception:
        db.rollback()
        raise


def write_chapter(db: Session, conversation_id: str, message_id: str, request: WriteChapterRequest):
    """仅显式确认的助手消息可建章；默认承接当前最新章。"""
    try:
        conv, _ = source_message(db, conversation_id, message_id)
        existing = db.query(models.ChatAction).filter(
            models.ChatAction.message_id == message_id,
            models.ChatAction.action_type == "chapter",
        ).first()
        if existing:
            if existing.novel_id != conv.novel_id:
                raise HTTPException(status_code=409, detail="该动作属于其他作品")
            chapter = writing.ChapterRepo(db).get(conv.novel_id, existing.result_id)
            yield writing.sse({"type": "done", "chapter": writing._chapter_dict(chapter)})
            return
        if request.outline_node_id:
            node = db.query(models.OutlineNode).filter(
                models.OutlineNode.id == request.outline_node_id,
                models.OutlineNode.novel_id == conv.novel_id,
                models.OutlineNode.level == "chapter",
            ).first()
            if not node:
                raise HTTPException(status_code=400, detail="所选章级大纲节点不属于当前作品")
        chapters = writing.ChapterRepo(db).list(conv.novel_id)
        latest_id = chapters[-1].id if chapters else ""
        previous_id = request.previous_chapter_id or latest_id
        if previous_id != latest_id:
            raise HTTPException(status_code=400, detail="只能承接当前最新章节")
        yield from writing.write_chapter(
            db, conv.novel_id, directive=request.directive, title=request.title,
            outline_node_id=request.outline_node_id,
            previous_chapter_id=previous_id, action_message_id=message_id,
            expected_latest_id=latest_id,
        )
    except Exception as exc:  # noqa: BLE001  SSE 流内保持错误事件协议
        db.rollback()
        yield writing.sse({"type": "error", "message": str(exc)})
