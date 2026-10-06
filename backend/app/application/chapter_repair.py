"""整章修订：只读预览，确认时用数据库条件更新保护作者原稿。"""
import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.application.context import prior_chapters
from app.application.revisions import (
    SOURCE_REWRITE_APPLY, delete_chapter_memory, snapshot_chapter,
)
from app.application.story_review import _bible, build_requirement_plan
from app.application.word_count import check_word_limits
from app.config import get_settings
from app.infrastructure import models, repository as repos
from app.infrastructure.ai.llm_provider import LLMProvider
from app.infrastructure.ai.prompts import CHAPTER_REPAIR_SYSTEM, CHAPTER_REPAIR_USER
from app.interfaces.schemas import (
    ChapterRepairApplyRequest, ChapterRepairPreview, ChapterRepairPreviewRequest, WordCountResult,
)

llm = LLMProvider()
MAX_ORIGINAL_CHARS = 20000
MAX_MATERIALS_CHARS = 40000
MAX_REPAIR_CHARS = 40000


def _row_values(row) -> dict:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def _base_materials(db: Session, novel_id: str, chapter_id: str) -> tuple[object, dict]:
    """全文和所有资料完整读取；不把被省略的片段用于替换整章。"""
    chapter = repos.ChapterRepo(db).get(novel_id, chapter_id)
    bible = json.loads(_bible(db, novel_id))
    prior = prior_chapters(db, novel_id, chapter.seq, chapter.outline_node_id)[-3:]
    outline = None
    if chapter.outline_node_id:
        outline = repos.OutlineRepo(db).get(novel_id, chapter.outline_node_id)
    base = {
        "chapter": _row_values(chapter),
        "Story Bible（唯一权威设定）": bible,
        "前章资料（草稿及摘要可能有错，不覆盖设定）": [_row_values(row) for row in prior],
        "本章绑定大纲": _row_values(outline) if outline is not None else None,
    }
    return chapter, base


def _fingerprint(base: dict) -> str:
    encoded = json.dumps(base, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _word_count(content: str, limits: dict | None) -> WordCountResult:
    counted = check_word_limits(content, limits)
    return WordCountResult(count=counted.pop("word_count"), **counted)


def preview_repair(
    db: Session, novel_id: str, chapter_id: str, request: ChapterRepairPreviewRequest, *, provider=None,
) -> ChapterRepairPreview:
    """单次补全生成修订稿，不调用检查服务、不写任何表。"""
    with db.no_autoflush:
        chapter, base = _base_materials(db, novel_id, chapter_id)
        original = chapter.content
        title, directive = chapter.title, chapter.directive
        if not original.strip():
            raise HTTPException(status_code=400, detail="已保存正文为空，请先保存原稿再修订")
        if len(original) > MAX_ORIGINAL_CHARS:
            raise HTTPException(status_code=413, detail="已保存正文超过整章修订上限，请分段修订；未截断或改动原稿")
        outline = base["本章绑定大纲"]
        plan = build_requirement_plan(directive, outline["summary"] if outline and outline["level"] == "chapter" else "")
        known_ids = {item.id for item in plan}
        pending_ids = list(dict.fromkeys(request.unreviewed_requirement_ids))
        if any(item not in known_ids for item in pending_ids):
            raise HTTPException(status_code=400, detail="待核对条目已变化，请重新检查构思后生成修订稿")
        limits = request.word_limits.model_dump() if request.word_limits is not None else None
        original_count = _word_count(original, limits)
        materials = {
            **base,
            "程序计算的完整正文字数": {
                **original_count.model_dump(),
                "计数口径": "完整原文的非空白 Unicode 字符数，含标点，空格、换行及其他空白不计",
            },
            "作者本次修订要求": request.instructions,
            "作者原文逐项要求": [
                {"id": item.id, "source": item.source, "text": item.text, "context": item.context}
                for item in plan
            ],
            "AI核对提示（可能有误，须核实，不是新增事实或要求）": [issue.model_dump() for issue in request.issues],
            "待核对要求ID（只验证，不代表违规）": pending_ids,
        }
        serialized = json.dumps(materials, ensure_ascii=False)
        if len(serialized) > MAX_MATERIALS_CHARS:
            raise HTTPException(status_code=413, detail="整章修订资料超过上限，请减少单次修订范围；未截断或改动原稿")
        fingerprint = _fingerprint(base)

    # 模型等待期间释放只读事务；应用时会重读并验证，不持数据库写锁。
    if not (db.new or db.dirty or db.deleted):
        db.rollback()
    try:
        content = (llm if provider is None else provider).complete(
            [{"role": "system", "content": CHAPTER_REPAIR_SYSTEM},
             {"role": "user", "content": CHAPTER_REPAIR_USER.format(materials=serialized)}],
            temperature=0.3, max_tokens=get_settings().writing_max_tokens, require_complete=True,
        )
    except Exception:
        raise HTTPException(status_code=502, detail="整章修订服务暂不可用，请稍后重试；原稿未改动") from None
    if not isinstance(content, str) or not content.strip() or len(content) > MAX_REPAIR_CHARS or content.lstrip().startswith("```"):
        raise HTTPException(status_code=502, detail="模型未返回有效的完整修订正文，请重试；原稿未改动")
    return ChapterRepairPreview(
        chapter_id=chapter_id, base_fingerprint=fingerprint, original_content=original,
        content=content, title=title, directive=directive,
        original_word_count=original_count, word_count=_word_count(content, limits),
    )


def apply_repair(
    db: Session, novel_id: str, chapter_id: str, request: ChapterRepairApplyRequest,
):
    """SQLite写锁内重读资料并CAS；快照、正文和记忆失效同事务完成。"""
    if not request.content.strip():
        raise HTTPException(status_code=400, detail="修订正文为空，无法应用")
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("应用修订前不能存在未提交修改")
    # 清除先前的只读事务和ORM缓存。SQLite锁覆盖读取、上下文校验、CAS及所有派生清理。
    db.rollback()
    try:
        if db.get_bind().dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))
        db.expire_all()
        chapter, base = _base_materials(db, novel_id, chapter_id)
        if _fingerprint(base) != request.base_fingerprint:
            raise HTTPException(status_code=409, detail="原稿或修订依据已变化，请重新生成预览；当前稿未被覆盖")
        if chapter.content == request.content:
            db.rollback()
            db.refresh(chapter)
            return chapter
        original_fields = base["chapter"]
        conditions = [getattr(models.Chapter, key) == value for key, value in original_fields.items()]
        changed = db.execute(
            update(models.Chapter).where(*conditions).values(
                content=request.content, status="draft", summary="", tension_score=None, drift_score=None,
            ).execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            raise HTTPException(status_code=409, detail="原稿已变化，请重新生成预览；当前稿未被覆盖")
        # CAS未同步旧ORM值，因此这里准确留存被覆盖前的完整状态。
        snapshot_chapter(db, chapter, SOURCE_REWRITE_APPLY)
        delete_chapter_memory(db, novel_id, chapter_id)
        db.commit()
        db.refresh(chapter)
        return chapter
    except OperationalError:
        db.rollback()
        raise HTTPException(status_code=409, detail="作品正在保存，请稍后重新核对修订预览；当前稿未被覆盖") from None
    except Exception:
        db.rollback()
        raise
