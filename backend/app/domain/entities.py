"""领域层值对象与常量。

阶段一只涉及伏笔状态枚举；六类实体的字段定义见 app/infrastructure/models.py（ORM 实体）。
"""
from enum import Enum


class ForeshadowStatus(str, Enum):
    """伏笔状态机。"""
    PLANTED = "planted"      # 已埋
    SUSPENDED = "suspended"  # 已悬置
    CONSUMED = "consumed"    # 已消费
