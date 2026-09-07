"""领域层值对象与常量。"""
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
    VOLUME = "volume"
    ACT = "act"
    CHAPTER = "chapter"
