"""领域层值对象与常量。

六类设定实体与章节的字段定义见 app/infrastructure/models.py（ORM 实体）。
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
