"""领域层值对象与常量。

六类设定实体、章节与大纲节点的字段定义见 app/infrastructure/models.py（ORM 实体）。
"""
from enum import Enum


class ForeshadowStatus(str, Enum):
    """伏笔状态机。"""
    PLANTED = "planted"      # 已埋
    SUSPENDED = "suspended"  # 已悬置
    CONSUMED = "consumed"    # 已消费


class ChapterStatus(str, Enum):
    """章节状态。"""
    DRAFT = "draft"          # 草稿
    FINALIZED = "finalized"  # 定稿


class OutlineLevel(str, Enum):
    """大纲层级。"""
    VOLUME = "volume"        # 卷
    ACT = "act"              # 幕
    CHAPTER = "chapter"      # 章


class RevisionSource(str, Enum):
    """章节版本来源中**允许调用方指定**的部分。

    manual_save / rewrite_apply 可由前端在 PUT 时指定；
    continue / rewrite / restore 只能由后端写作用例自行决定，不接受外部传入。
    对应的字符串常量见 app/application/revisions.py 的 SOURCE_*。
    """
    MANUAL_SAVE = "manual_save"        # 手工保存
    REWRITE_APPLY = "rewrite_apply"    # 改写预览后应用
