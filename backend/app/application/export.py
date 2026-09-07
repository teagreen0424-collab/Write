"""导出用例：整本 Markdown / TXT。"""
from sqlalchemy.orm import Session

from app.infrastructure import repository as repos


def export_markdown(db: Session, novel_id: str) -> str:
    novel = repos.NovelRepo(db).get(novel_id)
    parts = [f"# {novel.title}\n"]
    for c in repos.ChapterRepo(db).finalized(novel_id):
        parts.append(f"## 第{c.seq}章 {c.title}\n\n{c.content}\n")
    return "\n".join(parts)


def export_txt(db: Session, novel_id: str) -> str:
    novel = repos.NovelRepo(db).get(novel_id)
    parts = [novel.title, ""]
    for c in repos.ChapterRepo(db).finalized(novel_id):
        parts.append(f"第{c.seq}章 {c.title}\n{c.content}\n")
    return "\n".join(parts)
