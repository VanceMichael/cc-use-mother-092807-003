"""领域服务的错误类型。"""

from __future__ import annotations


class RestorationError(Exception):
    """所有业务规则错误的基类。"""


class PermissionDenied(RestorationError):
    """岗位无权执行该操作。"""


class ConflictError(RestorationError):
    """空间重叠或用途冲突。"""

    def __init__(self, message: str, *, conflicts: list[dict] | None = None) -> None:
        super().__init__(message)
        self.conflicts = conflicts or []


class FrozenMethodError(RestorationError):
    """违反采集方法或适用季节冻结约束。"""


class ReviewRequired(RestorationError):
    """观测值为异文数据，进入复核而非直接采纳。"""


class ImmutableResultError(RestorationError):
    """已确认结果不得被新模型改写。"""


class SeparationOfDutiesError(RestorationError):
    """提交、确认、付款之间的职责分离被破坏。"""


class MilestoneStateError(RestorationError):
    """里程碑当前状态不允许该操作。"""
