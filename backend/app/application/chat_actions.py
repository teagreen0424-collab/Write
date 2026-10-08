"""聊天来源校验与动作去重。"""
from fastapi import HTTPException
from sqlalchemy import literal_column
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.application import outline_plan
from app.application import writing
from app.application import inject_idea
from app.application import generated_chapter, generated_consistency
from app.infrastructure import models
from app.infrastructure.repository import ConversationReadError, ConversationRepo
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


def list_actions_batch(db: Session, conversation_id: str, message_ids: list[str]) -> dict:
    """一次核对全部来源，再批量读取原动作与本作品目标，不触发任何落地操作。"""
    if (not 1 <= len(message_ids) <= 100
            or any(not isinstance(mid, str) or not 1 <= len(mid) <= 200 for mid in message_ids)
            or len(set(message_ids)) != len(message_ids)):
        raise ConversationReadError(422, "请提供1至100个不重复的消息身份", "chat_action_batch_invalid")
    scope = ConversationRepo(db).get_scope(conversation_id)
    sources = db.query(models.Message.id).filter(
        models.Message.conversation_id == conversation_id, models.Message.role == "assistant",
        models.Message.id.in_(message_ids),
    ).all()
    if len(sources) != len(message_ids):
        raise ConversationReadError(404, "部分来源不是当前会话中的助手消息", "chat_action_sources_not_found")
    result = {mid: [] for mid in message_ids}
    if scope.novel_id is None:
        return {"conversation_id": scope.id, "novel_id": None, "actions_by_message_id": result}
    action = models.ChatAction
    actions = db.query(action.message_id, action.action_type, action.status, action.result_id, action.result_summary).filter(
        action.novel_id == scope.novel_id, action.message_id.in_(message_ids),
    ).order_by(action.created_at, literal_column("chat_action.rowid")).all()
    outline_ids = {row.result_id for row in actions if row.action_type == "outline"}
    chapter_ids = {row.result_id for row in actions if row.action_type == "chapter"}
    present_outlines = {row.id for row in db.query(models.OutlineNode.id).filter(
        models.OutlineNode.novel_id == scope.novel_id, models.OutlineNode.id.in_(outline_ids),
    ).all()} if outline_ids else set()
    present_chapters = {row.id for row in db.query(models.Chapter.id).filter(
        models.Chapter.novel_id == scope.novel_id, models.Chapter.id.in_(chapter_ids),
    ).all()} if chapter_ids else set()
    for row in actions:
        deleted = ((row.action_type == "outline" and row.result_id not in present_outlines)
            or (row.action_type == "chapter" and row.result_id not in present_chapters))
        result[row.message_id].append({"action_type": row.action_type, "status": row.status,
            "result_id": row.result_id, "result_summary": row.result_summary, "target_deleted": deleted})
    return {"conversation_id": scope.id, "novel_id": scope.novel_id, "actions_by_message_id": result}


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
        generated_chapter.require_generation_session(db)
        db.expire_all()  # 干净只读会话也必须采用当前绑定，而不是重放旧缓存作品或章节。
        conv, _ = source_message(db, conversation_id, message_id)
        existing = generated_chapter.existing_chat_chapter(db, conv.novel_id, message_id)
        if existing is not None:
            yield writing.sse({"type": "done", "chapter": existing, "consistency": generated_consistency.result(
                None, conv.novel_id, existing["id"], existing["content"], "existing_action")})
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
    except SQLAlchemyError:
        yield writing._generation_storage_error()
    except Exception as exc:  # noqa: BLE001  SSE 流内保持错误事件协议
        yield writing._stream_error(exc)
