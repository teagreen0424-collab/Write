"""生成末尾的字面规则提示：提交前取事实，提交后仅计算返回正文。"""
from dataclasses import dataclass
import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.application import consistency
from app.infrastructure import models


@dataclass(frozen=True)
class RuleFacts:
    # Core Row 是已取出的不可变标量，不会触发 ORM 过期加载或提交后 SQL。
    foreshadowings: tuple
    characters: tuple
    events: tuple


def capture_rule_facts(db: Session, novel_id: str) -> RuleFacts | None:
    """模型结束、保存前投影当前三类事实；读取失败不否认后续保存结果。"""
    try:
        with db.no_autoflush:
            clue, person, timeline = (model.__table__ for model in
                (models.Foreshadowing, models.Character, models.TimelineEvent))
            foreshadowings = tuple(db.execute(select(clue.c.id, clue.c.status, clue.c.content).where(
                clue.c.novel_id == novel_id).order_by(clue.c.created_at)).all())
            characters = tuple(db.execute(select(person.c.id, person.c.name, person.c.known_info).where(
                person.c.novel_id == novel_id).order_by(person.c.created_at)).all())
            events = tuple(db.execute(select(timeline.c.id, timeline.c.title, timeline.c.cause_id).where(
                timeline.c.novel_id == novel_id).order_by(timeline.c.created_at)).all())
        return RuleFacts(foreshadowings, characters, events)
    except Exception:  # 不把 SQL、参数或原始设定带入作者提示，也不改调用方事务。
        return None


def result(facts: RuleFacts | None, novel_id: str, chapter_id: str, content: str, subject: str) -> dict:
    """绑定完整实际返回正文；没有数据库、模型、嵌入或网络操作。"""
    value = {"status": "failed", "novel_id": novel_id, "chapter_id": chapter_id,
        "subject": subject, "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "offset_unit": "unicode_codepoint", "conflicts": [], "warnings": []}
    if subject == "existing_action":
        value.update(status="skipped", warnings=["本次返回已有聊天动作，未重新生成或运行规则；请按需手动核对当前正文。"])
        return value
    try:
        if facts is None:
            raise ValueError("规则事实未完成读取")
        conflicts = consistency.rule_conflicts(content, facts.foreshadowings, facts.characters, facts.events)
        value.update(status="completed", conflicts=conflicts,
            warnings=["仅运行伏笔状态、人物已知信息和时间线的三类字面规则；未命中不代表语义一致。"])
    except Exception:
        value["warnings"] = ["确定性规则检查未完成，请手动核对；此状态不改变章节保存或预览结果。"]
    return value
